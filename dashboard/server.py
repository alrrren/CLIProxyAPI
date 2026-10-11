import csv
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import threading
import time
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo
import ccusage_data

ROOT = Path.home() / '.config/cliproxyapi'
ZONE = ZoneInfo('America/Mexico_City')
UPDATE_LOCK = threading.Lock()
UPDATE_CACHE = {}


def upstream_status():
    with UPDATE_LOCK:
        if UPDATE_CACHE and time.monotonic() < UPDATE_CACHE['expires']:
            return UPDATE_CACHE['result']
        build = json.loads(Path(__file__).with_name('build.json').read_text())
        result = dict(build, status='unknown', checked_at=datetime.now(ZONE).isoformat())
        try:
            def github(path):
                request = Request('https://api.github.com/repos/router-for-me/CLIProxyAPI/' + path,
                                  headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'CPA-Pool-Dashboard'})
                with urlopen(request, timeout=8) as response:
                    return json.load(response)
            tip = github('commits/HEAD')['sha']
            comparison = github('compare/' + build['base_commit'] + '...' + tip)
            state = comparison['status']
            result.update(tip_commit=tip, commits_behind=comparison['ahead_by'],
                          status='current' if state == 'identical' else 'outdated' if state == 'ahead' else 'diverged',
                          compare_url='https://github.com/router-for-me/CLIProxyAPI/compare/' + build['base_commit'] + '...' + tip)
        except (OSError, ValueError, KeyError):
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
    timestamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).astimezone(ZONE)
    return dict(timestamp=timestamp.isoformat(), date=timestamp.date().isoformat(), hour=timestamp.hour,
                account=record.get('source') or 'Unknown account', model=record.get('model') or 'Unknown model',
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
                cache_write=sum(r['write'] or 0 for r in known), cache_covered=len(known),
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


def load(query):
    days = int(query.get('days', ['7'])[0])
    if days not in (1, 7, 30, 90):
        raise ValueError('Unsupported date range')
    now = datetime.now(ZONE)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    with sqlite3.connect(f'file:{ROOT / "usage.sqlite3"}?mode=ro', uri=True) as db:
        all_rows = [normalize(json.loads(row[0])) for row in db.execute('SELECT record FROM requests')]
    options = dict(accounts=sorted({r['account'] for r in all_rows}), models=sorted({r['model'] for r in all_rows}))
    rows = [r for r in all_rows if start <= datetime.fromisoformat(r['timestamp']) <= now]
    for field in ('account', 'model'):
        selected = query.get(field, [''])[0]
        if selected:
            rows = [r for r in rows if r[field] == selected]
    groups = {}
    for field in ('account', 'model'):
        groups[field] = [dict(name=name, **summarize([r for r in rows if r[field] == name]))
                         for name in sorted({r[field] for r in rows})]
    daily = []
    for offset in range(days):
        date = (start + timedelta(days=offset)).date().isoformat()
        daily.append(dict(date=date, **summarize([r for r in rows if r['date'] == date])))
    hours = [sum(r['hour'] == hour for r in rows) for hour in range(24)]
    status_file = ROOT / 'collector-status.json'
    health = json.loads(status_file.read_text()) if status_file.exists() else {}
    stamp = health.get('last_success')
    age = max(0, (now - datetime.fromisoformat(stamp)).total_seconds()) if stamp else None
    data = dict(summary=summarize(rows), account_activity=account_activity(rows), groups=groups, daily=daily, hours=hours, options=options,
                recent=sorted(rows, key=lambda r: r['timestamp'], reverse=True)[:100],
                generated=now.isoformat(), timezone=str(ZONE), collector_age=age,
                history_start=min((r['timestamp'] for r in all_rows), default=None),
                last_request=max((r['timestamp'] for r in all_rows), default=None))
    return data, rows


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path in ('/', '/index.html'):
                content = Path(__file__).with_name('index.html').read_bytes()
                mime = 'text/html; charset=utf-8'
            elif parsed.path == '/api/accounts':
                content = (ROOT / 'account-pool.json').read_bytes()
                mime = 'application/json'
            elif parsed.path == '/api/ccusage':
                content = json.dumps(ccusage_data.dashboard(), allow_nan=False).encode()
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
        except ValueError:
            self.send_error(400, 'Invalid filters')
        except (sqlite3.Error, OSError):
            self.send_error(503, 'Usage storage unavailable')

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    threading.Thread(target=ccusage_data.collect, daemon=True).start()
    threading.Thread(target=ccusage_data.collect, args=(ccusage_data.pull_genius,), daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', 8320), Handler).serve_forever()
