import csv
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo
import ccusage_data

ROOT = Path.home() / '.config/cliproxyapi'
ZONE = ZoneInfo(ccusage_data.ZONE)
UPDATE_LOCK = threading.Lock()
UPDATE_CACHE = {}
HISTORY = ROOT / 'dashboard/quota-history.sqlite3'
# The listener's own names and the Tailscale Serve name that forwards to it; any other Host is a rebound DNS name.
HOSTS = {'127.0.0.1:8320', 'localhost:8320', 'aleksmbp.tail035f92.ts.net:8321'}


def upstream_status():
    with UPDATE_LOCK:
        if UPDATE_CACHE and time.monotonic() < UPDATE_CACHE['expires']:
            return UPDATE_CACHE['result']
        result = dict(status='unknown', checked_at=datetime.now(ZONE).isoformat())
        try:
            build = json.loads((ROOT / 'dashboard/build.json').read_text())
            result['version'] = build['version']
            def github(path):
                request = Request('https://api.github.com/repos/router-for-me/CLIProxyAPI/' + path,
                                  headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'CPA-Pool-Dashboard'})
                with urlopen(request, timeout=8) as response:
                    return json.load(response)
            tip = github('commits/HEAD')['sha']
            comparison = github('compare/' + build['base_commit'] + '...' + tip)
            state = comparison['status']
            result.update(commits_behind=comparison['ahead_by'],
                          status='current' if state == 'identical' else 'outdated' if state == 'ahead' else 'diverged',
                          compare_url='https://github.com/router-for-me/CLIProxyAPI/compare/' + build['base_commit'] + '...' + tip)
        except (OSError, ValueError, KeyError, TypeError):
            result['error'] = 'Upstream check unavailable; retrying in five minutes.'
        UPDATE_CACHE.update(result=result, expires=time.monotonic() + 300)
        return result


def number(value):
    return max(0, value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def normalize(record):
    tokens = record.get('tokens', {})
    breakdown = record.get('token_breakdown', {})
    accurate = breakdown.get('quality') == 'complete'
    inp, out = breakdown.get('input', {}), breakdown.get('output', {})
    explicit = tokens.get('cache_read_tokens_present') is True
    output = number(out.get('total_tokens')) if accurate else number(tokens.get('output_tokens'))
    total = number(breakdown.get('total_tokens')) if accurate else number(tokens.get('total_tokens'))
    input_total = number(inp.get('total_tokens')) if accurate else max(0, total - output)
    read = number(inp.get('cache_read_tokens')) if accurate else number(tokens.get('cache_read_tokens')) if explicit else None
    write = number(inp.get('cache_write_tokens')) if accurate else number(tokens.get('cache_creation_tokens')) if explicit else None
    timestamp = ccusage_data.stamp(record['timestamp']).astimezone(ZONE)
    account, model = record.get('source'), record.get('model')
    if not all(isinstance(value, (str, type(None))) for value in (account, model)):
        raise TypeError('Stored account and model must be text')
    account, model = account or 'Unknown account', model or 'Unknown model'
    return dict(timestamp=timestamp.isoformat(), date=timestamp.date().isoformat(), hour=timestamp.hour,
                account=account, model=model,
                input=input_total, output=output, total=total, read=read, write=write,
                duration=number(record.get('latency_ms')) / 1000,
                ttft=number(record.get('ttft_ms')) / 1000 if number(record.get('ttft_ms')) > 0 else None,
                failed=bool(record.get('failed')), status=record.get('status_code'), request_id=record.get('request_id'))


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summarize(rows):
    known = [r for r in rows if r['read'] is not None]
    input_known = sum(r['input'] for r in known)
    return dict(requests=len(rows), failures=sum(r['failed'] for r in rows),
                total=sum(r['total'] for r in rows), input=sum(r['input'] for r in rows),
                output=sum(r['output'] for r in rows), cached=sum(r['read'] for r in known),
                cache_write=sum(r['write'] for r in known), cache_covered=len(known),
                cache_rate=sum(r['read'] for r in known) / input_known if input_known else None,
                p50=percentile([r['duration'] for r in rows], .5),
                p95=percentile([r['duration'] for r in rows], .95),
                ttft=percentile([r['ttft'] for r in rows if r['ttft'] is not None], .5),
                ttft_covered=sum(r['ttft'] is not None for r in rows))


def account_activity(rows):
    ordered = sorted(rows, key=lambda r: r['timestamp'])
    comparable = [(a, b) for a, b in zip(ordered, ordered[1:])
                  if a['timestamp'] != b['timestamp'] and
                  a['account'] != 'Unknown account' and b['account'] != 'Unknown account']
    changed = [(a, b) for a, b in comparable if a['account'] != b['account']]
    return dict(changes=len(changed), comparisons=len(comparable),
                last_change=changed[-1][1]['timestamp'] if changed else None)


def readonly(path):
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


# json_extract stops the whole query at the first invalid record, so SQL reads fields only from valid JSON.
RECORD = 'CASE WHEN json_valid(record) THEN record END'


def readable(texts):
    """Each stored record that normalizes, with its row, and how many of the rest could not be read."""
    pairs = []
    for (text,) in texts:
        try:
            record = json.loads(text)
            row = normalize(record)
            # A NaN or infinite value anywhere would make the whole JSON response fail.
            json.dumps(record, allow_nan=False)
            pairs.append((record, row))
        except (*ccusage_data.MALFORMED, RecursionError):
            pass
    return pairs, len(texts) - len(pairs)


def cpa_from_genius():
    """Daily tokens Genius sent through CPA, counted only since the collector began keeping each request's source."""
    try:
        genius = {info[4][0] for info in socket.getaddrinfo(urlparse(ccusage_data.GENIUS_URL).hostname, None)}
    except OSError:
        return None
    with readonly(ROOT / 'usage.sqlite3') as db:
        rows, skipped = readable(db.execute(
            f"SELECT record FROM requests WHERE json_type({RECORD}, '$.client') = 'text' OR NOT json_valid(record)").fetchall())
    days = {}
    for record, row in rows:
        # The split subtracts this from Genius's Claude-only ccusage, so other providers must not count.
        if record['client'] in genius and record.get('provider') == 'claude' and not row['failed']:
            days[row['date']] = days.get(row['date'], 0) + row['total']
    return dict(since=min((row['timestamp'] for _, row in rows), default=None), days=days, skipped=skipped)


class InvalidFilter(Exception):
    pass


def load(query):
    days = query.get('days', ['7'])[0]
    if days not in ('1', '7', '30', '90'):
        raise InvalidFilter
    days = int(days)
    now = datetime.now(ZONE)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Only records near the period, or without a date SQL can read, are parsed; the one-day margin covers records
    # written in another UTC offset, and the exact range check below still applies.
    margin = (start - timedelta(days=1)).date().isoformat()
    with readonly(ROOT / 'usage.sqlite3') as db:
        # Undated records are read too, so they are counted as unreadable rather than silently dropped.
        rows, skipped = readable(db.execute(
            f"SELECT record FROM (SELECT record, json_extract({RECORD}, '$.timestamp') AS at FROM requests) "
            "WHERE NOT coalesce(at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*', 0) OR substr(at, 1, 10) >= ?",
            (margin,)).fetchall())
        options = {key: [name for (name,) in db.execute(
            f"SELECT DISTINCT coalesce(nullif(json_extract(record, '$.{field}'), ''), ?) FROM requests "
            f"WHERE json_type({RECORD}) = 'object' AND coalesce(json_type({RECORD}, '$.{field}'), 'null') IN ('text', 'null') "
            "ORDER BY 1",
            (empty,))]
            for key, field, empty in (('accounts', 'source', 'Unknown account'), ('models', 'model', 'Unknown model'))}
        stamps = []
        for (at,) in db.execute(f"SELECT json_extract({RECORD}, '$.timestamp') FROM requests"):
            try:
                stamps.append(ccusage_data.stamp(at))
            except ccusage_data.MALFORMED:
                pass
    rows = [r for _, r in rows if start <= datetime.fromisoformat(r['timestamp']) <= now]
    for field in ('account', 'model'):
        selected = query.get(field, [''])[0]
        if selected:
            rows = [r for r in rows if r[field] == selected]
    groups = {}
    for field in ('account', 'model'):
        groups[field] = [dict(name=name, **summarize([r for r in rows if r[field] == name]))
                         for name in sorted({r[field] for r in rows})]
    by_date, hours = {}, [0] * 24
    for r in rows:
        by_date.setdefault(r['date'], []).append(r)
        hours[r['hour']] += 1
    dates = [(start + timedelta(days=offset)).date().isoformat() for offset in range(days)]
    daily = [dict(date=date, **summarize(by_date.get(date, []))) for date in dates]
    status_file = ROOT / 'collector-status.json'
    health = json.loads(status_file.read_text()) if status_file.exists() else {}
    stamp = health.get('last_success')
    age = max(0, (now - datetime.fromisoformat(stamp)).total_seconds()) if stamp else None
    data = dict(summary=summarize(rows), account_activity=account_activity(rows), groups=groups, daily=daily, hours=hours, options=options,
                recent=sorted(rows, key=lambda r: r['timestamp'], reverse=True)[:100],
                generated=now.isoformat(), collector_age=age, skipped=skipped,
                history_start=min(stamps).astimezone(ZONE).isoformat() if stamps else None)
    return data, rows


def accounts():
    pool = json.loads((ROOT / 'account-pool.json').read_bytes())
    # Aleks uses the work account on Genius, so CPA's disabled copy of it is noise.
    pool['accounts'] = [a for a in pool['accounts'] if not (a.get('name') == 'Work' and a.get('disabled'))]
    work = ccusage_data.genius_account()
    if work:
        pool['accounts'].append(work)
    return pool


def epoch(stamp):
    return ccusage_data.stamp(stamp).timestamp()


def record_quota():
    """Keeps each new 7-day reading, so pace can follow recent use instead of the whole window's average."""
    rows = [(a['name'], epoch(a['observed_at']), a['seven_day']['utilization'], epoch(a['seven_day']['resets_at']))
            for a in accounts()['accounts']
            if not a.get('disabled') and not a.get('quota_error') and a.get('observed_at') and a.get('seven_day') and
            isinstance(a['seven_day'].get('utilization'), (int, float)) and a['seven_day'].get('resets_at')]
    with sqlite3.connect(HISTORY) as db:
        db.execute('CREATE TABLE IF NOT EXISTS readings (account TEXT, at REAL, used REAL, reset REAL, PRIMARY KEY (account, at))')
        db.executemany('INSERT OR IGNORE INTO readings VALUES (?, ?, ?, ?)', rows)
        db.execute('DELETE FROM readings WHERE at < ?', (time.time() - 8 * 86400,))


def with_history(pool):
    if not HISTORY.exists():
        return pool
    # History only refines pace, so an unreadable store must not hide the quota readings themselves.
    try:
        with readonly(HISTORY) as db:
            readings = db.execute('SELECT account, at, used, reset FROM readings ORDER BY at').fetchall()
    except sqlite3.Error:
        return pool
    for account in pool['accounts']:
        week = account.get('seven_day') or {}
        reset = epoch(week['resets_at']) if week.get('resets_at') else None
        # Only readings from the current window describe its pace; the reset time can drift by seconds between readings.
        account['history'] = [[at * 1000, used] for name, at, used, r in readings
                              if name == account['name'] and reset and abs(r - reset) < 3600]
    return pool


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get_all('Host') or []
        if len(host) != 1 or host[0].lower() not in HOSTS:
            self.send_error(403)
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path in ('/', '/index.html'):
                content = Path(__file__).with_name('index.html').read_bytes()
                mime = 'text/html; charset=utf-8'
            elif parsed.path == '/api/accounts':
                content = json.dumps(with_history(accounts()), allow_nan=False).encode()
                mime = 'application/json'
            elif parsed.path == '/api/ccusage':
                content = json.dumps(dict(ccusage_data.dashboard(), cpaFromGenius=cpa_from_genius()), allow_nan=False).encode()
                mime = 'application/json'
            elif parsed.path == '/api/upstream':
                content = json.dumps(upstream_status()).encode()
                mime = 'application/json'
            elif parsed.path in ('/api/data', '/api/export.csv'):
                data, rows = load(parse_qs(parsed.query))
                if parsed.path == '/api/data':
                    content = json.dumps(data, allow_nan=False).encode()
                    mime = 'application/json'
                else:
                    buffer = io.StringIO()
                    fields = ('timestamp', 'account', 'model', 'input', 'output', 'total', 'read', 'write', 'duration', 'ttft', 'failed', 'status', 'request_id')
                    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction='ignore')
                    writer.writeheader()
                    for row in rows:
                        safe = {k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for k, v in row.items()}
                        writer.writerow(safe)
                    content = buffer.getvalue().encode()
                    mime = 'text/csv; charset=utf-8'
            elif parsed.path == '/favicon.ico':
                self.send_response(204)
                self.end_headers()
                return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except InvalidFilter:
            self.send_error(400, 'Invalid filters')
        except (sqlite3.Error, OSError) as error:
            # Path only: the query string carries filter values.
            print(f'{parsed.path} failed: {type(error).__name__}', file=sys.stderr, flush=True)
            self.send_error(503, 'Usage storage unavailable')
        except ccusage_data.MALFORMED as error:
            print(f'{parsed.path} failed: {type(error).__name__}', file=sys.stderr, flush=True)
            self.send_error(500, 'Dashboard data unreadable')

    # Request lines would carry filter query strings into the log.
    def log_message(self, *_):
        pass


if __name__ == '__main__':
    threading.Thread(target=ccusage_data.collect, daemon=True).start()
    threading.Thread(target=ccusage_data.collect, args=(ccusage_data.pull_genius,), daemon=True).start()
    threading.Thread(target=ccusage_data.collect, args=(record_quota,), daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', 8320), Handler).serve_forever()
