"""Aggregate ccusage snapshots only; no transcript or SQLite parsing."""
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

# Runtime data stays in the config directory so the code can live in a Git checkout.
STORE = Path.home() / '.config/cliproxyapi/dashboard/ccusage-snapshots'
MACHINES = ('aleks-mbp', 'genius')
ZONE = 'America/Mexico_City'
COMMAND = ['daily', '--json', '--by-agent', '--offline', '--no-cost', '--timezone', ZONE]
COUNTERS = ('inputTokens', 'cacheCreationTokens', 'cacheReadTokens', 'outputTokens', 'totalTokens')
SEMANTICS = 'input_excludes_cache; total=input+cacheCreation+cacheRead+output'
last_full_capture = 0.0
GENIUS_URL = 'https://genius.tail035f92.ts.net:8444/snapshot.json'
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
LIMITATIONS = [
    'Only usage reported by ccusage; no independent completeness guarantee.',
    'Copied histories can overlap across machines; machine totals describe local records.',
]


def stamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp requires timezone')
    return result


def utcnow():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def quota_window(window):
    if window is None:
        return None
    used, resets = window['usedPercentage'], window['resetsAt']
    if type(used) not in (int, float) or not 0 <= used <= 100 or type(resets) is not int or not 0 < resets < 2 ** 32:
        raise ValueError('Invalid quota window')
    return dict(usedPercentage=used, resetsAt=resets)


def quota_block(quota):
    if stamp(quota['observedAt']) > datetime.now(timezone.utc) + timedelta(minutes=5) or \
            quota['source'] != 'claude-code-statusline':
        raise ValueError('Invalid quota block')
    return dict(source=quota['source'], observedAt=quota['observedAt'],
                fiveHour=quota_window(quota['fiveHour']), sevenDay=quota_window(quota['sevenDay']))


def validate(snapshot):
    machine = snapshot['machine']['id']
    if (type(snapshot['schemaVersion']) is not int or snapshot['schemaVersion'] != 1 or machine not in MACHINES or
            snapshot['timezone'] != ZONE or snapshot['status'] not in ('ok', 'error') or
            snapshot['source']['tool'] != 'ccusage' or snapshot['source']['offline'] is not True or
            snapshot['source']['costsIncluded'] is not False or
            snapshot['coverage']['counterSemantics'] != SEMANTICS):
        raise ValueError('Unsupported snapshot')
    if stamp(snapshot['generatedAt']) > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError('Future snapshot')
    updated = snapshot['dataUpdatedAt']
    if updated and stamp(updated) > stamp(snapshot['generatedAt']):
        raise ValueError('Invalid update time')
    version = snapshot['source']['version']
    if not isinstance(version, str) or not re.fullmatch(r'[0-9A-Za-z.+_-]{1,40}', version):
        raise ValueError('Invalid version')
    rows, seen = [], set()
    if not isinstance(snapshot['rows'], list) or len(snapshot['rows']) > 50000:
        raise ValueError('Too many rows')
    for row in snapshot['rows']:
        if row['machineId'] != machine or row['app'] not in ('claude', 'codex'):
            raise ValueError('Invalid row origin')
        day = date.fromisoformat(row['date']).isoformat()
        model = row['model']
        if model is not None and (not isinstance(model, str) or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}', model) or '..' in model):
            raise ValueError('Invalid model')
        counts = {key: row[key] for key in COUNTERS}
        if any(type(v) is not int or not 0 <= v <= 9007199254740991 for v in counts.values()):
            raise ValueError('Invalid counter')
        if counts['totalTokens'] != sum(counts[k] for k in COUNTERS[:-1]):
            raise ValueError('Inconsistent total')
        if type(row['reportedInLatest']) is not bool:
            raise ValueError('Invalid reporting flag')
        if stamp(row['lastReportedAt']) > stamp(snapshot['generatedAt']):
            raise ValueError('Invalid row time')
        key = (machine, row['app'], day, model)
        if key in seen:
            raise ValueError('Duplicate aggregate')
        seen.add(key)
        rows.append(dict(machineId=machine, app=row['app'], date=day, model=model,
                         **counts, lastReportedAt=row['lastReportedAt'], reportedInLatest=row['reportedInLatest']))
    errors = []
    for error in snapshot.get('errors', []):
        # External messages, commands and hostnames never enter the saved snapshot.
        stamp(error['at'])
        errors.append(dict(source='ccusage', code='capture_failed', at=error['at']))
    if snapshot['status'] == 'error' and not errors:
        errors.append(dict(source='ccusage', code='capture_failed', at=snapshot['generatedAt']))
    quota = None
    if machine == 'genius' and snapshot.get('quota') is not None:
        # A malformed quota reading must not discard the usage rows that arrived with it.
        try:
            quota = quota_block(snapshot['quota'])
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
    return dict(schemaVersion=1, machine={'id': machine}, timezone=ZONE,
                generatedAt=snapshot['generatedAt'], dataUpdatedAt=updated, status=snapshot['status'],
                source=dict(tool='ccusage', version=version, offline=True, costsIncluded=False, command=COMMAND),
                coverage=dict(apps=sorted({r['app'] for r in rows}), scope='ccusage_reported_local_history',
                              historyPolicy='replace_app_day_models_preserve_absent_days', counterSemantics=SEMANTICS,
                              reportedRows=sum(r['reportedInLatest'] for r in rows),
                              retainedHistoricalRows=sum(not r['reportedInLatest'] for r in rows),
                              limitations=LIMITATIONS + (['Genius ccusage 20.0.20 has known modern-event coverage gaps.']
                                                         if machine == 'genius' and version == '20.0.20' else [])),
                rows=rows, errors=errors, quota=quota)


def read(machine):
    path = STORE / (machine + '.json')
    return validate(json.loads(path.read_text())) if path.exists() else None


def read_or_none(machine):
    """A stored file that no longer validates gives way to the next good snapshot instead of blocking it."""
    try:
        return read(machine)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def save(snapshot, since=None):
    snapshot = validate(snapshot)
    previous = read_or_none(snapshot['machine']['id'])
    if previous and stamp(snapshot['generatedAt']) < stamp(previous['generatedAt']):
        raise ValueError('Older snapshot cannot replace newer data')
    replaced = {(r['app'], r['date']) for r in snapshot['rows'] if r['reportedInLatest']}
    incoming = {(r['app'], r['date'], r['model']): r for r in snapshot['rows']}
    for row in (previous or {}).get('rows', []):
        key = (row['app'], row['date'], row['model'])
        if (row['app'], row['date']) not in replaced and key not in incoming:
            # Days before a partial report's range were not asked for, so they keep the last full report's verdict.
            incoming[key] = row if since and row['date'] < since else dict(row, reportedInLatest=False)
    snapshot['rows'] = sorted(incoming.values(), key=lambda r: (r['date'], r['app'], r['model'] or ''))
    write_json(STORE / (snapshot['machine']['id'] + '.json'), snapshot)
    return snapshot


def write_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
            temp = Path(handle.name)
            json.dump(data, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp and temp.exists():
            temp.unlink()


def capture():
    global last_full_capture
    now = utcnow()
    previous = read_or_none('aleks-mbp')
    # A full report takes seconds, so it runs hourly and after a failure; in between, recent days are enough.
    full = not previous or previous['status'] == 'error' or time.monotonic() - last_full_capture > 3600
    since = None if full else datetime.now(ZoneInfo(ZONE)).date() - timedelta(days=2)
    base = dict(schemaVersion=1, machine={'id': 'aleks-mbp'}, timezone=ZONE, generatedAt=now,
                coverage=dict(counterSemantics=SEMANTICS))
    try:
        result = subprocess.run(['/opt/homebrew/bin/ccusage', *COMMAND,
                                 *(['--since', since.strftime('%Y%m%d')] if since else [])],
                                capture_output=True, text=True, timeout=55, check=True)
        report = json.loads(result.stdout)
        version = subprocess.run(['/opt/homebrew/bin/ccusage', '--version'], capture_output=True,
                                 text=True, timeout=5, check=True).stdout.strip().split()[-1]
        rows = []
        for day in report['daily']:
            for app in day['agents']:
                if app['agent'] not in ('claude', 'codex'):
                    continue
                models = app.get('modelBreakdowns') or [dict(app, modelName=None)]
                for model in models:
                    counts = {key: model.get(key, 0) for key in COUNTERS[:-1]}
                    rows.append(dict(machineId='aleks-mbp', app=app['agent'], date=day['period'],
                                     model=model.get('modelName'), **counts, totalTokens=sum(counts.values()),
                                     lastReportedAt=now, reportedInLatest=True))
                if sum(r['totalTokens'] for r in rows[-len(models):]) != app['totalTokens']:
                    raise ValueError('Model breakdown does not match ccusage total')
        snapshot = save(dict(base, dataUpdatedAt=now, status='ok', rows=rows, errors=[],
                             source=dict(tool='ccusage', version=version, offline=True, costsIncluded=False)),
                        since=since and since.isoformat())
        if full:
            last_full_capture = time.monotonic()
        return snapshot
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        # save() carries the previous rows forward, marked as no longer reported.
        source = previous['source'] if previous else dict(tool='ccusage', version='unknown', offline=True,
                                                          costsIncluded=False)
        return save(dict(base, dataUpdatedAt=previous and previous['dataUpdatedAt'], status='error', source=source,
                         rows=[], errors=[dict(source='ccusage', code='capture_failed', at=now)]))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Snapshot redirects are not allowed')


def pull_genius():
    attempted = utcnow()
    health_file = STORE / 'genius-pull.json'
    previous = json.loads(health_file.read_text()) if health_file.exists() else {}
    health = dict(attemptedAt=attempted, lastSuccessAt=previous.get('lastSuccessAt'),
                  successfulPulls=previous.get('successfulPulls', 0), status='error')
    try:
        request = Request(GENIUS_URL, headers={'Accept': 'application/json', 'User-Agent': 'Pool-ccusage'})
        # Default HTTPS certificate verification stays enabled; redirects cannot change origin.
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            if response.status != 200 or response.geturl() != GENIUS_URL:
                raise ValueError('Unexpected snapshot origin or status')
            raw = response.read(MAX_SNAPSHOT_BYTES + 1)
            if len(raw) > MAX_SNAPSHOT_BYTES:
                raise ValueError('Snapshot exceeds four MiB')
        data = json.loads(raw)
        if data['machine']['id'] != 'genius':
            raise ValueError('Unexpected snapshot machine')
        snapshot = save(data)
        health.update(status='ok', lastSuccessAt=utcnow(), successfulPulls=health['successfulPulls'] + 1,
                      snapshotGeneratedAt=snapshot['generatedAt'], httpStatus=200)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        health['error'] = 'snapshot_pull_failed'
    write_json(health_file, health)
    return health


def collect(action=capture):
    while True:
        start = time.monotonic()
        try:
            action()
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            # Class name only: messages can carry snapshot or network details.
            print(f'{action.__name__} failed: {type(error).__name__}', file=sys.stderr, flush=True)
        time.sleep(max(1, 60 - (time.monotonic() - start)))


def genius_account():
    snapshot = read_or_none('genius')
    quota = snapshot and snapshot.get('quota')
    if not quota:
        return None
    window = lambda w: w and dict(utilization=w['usedPercentage'] / 100, status=None,
                                  resets_at=datetime.fromtimestamp(w['resetsAt'], timezone.utc).isoformat())
    return dict(name='Work · Genius', provider='claude', via='genius', disabled=False, status=None, priority=None,
                observed_at=quota['observedAt'], quota_error=None,
                five_hour=window(quota['fiveHour']), seven_day=window(quota['sevenDay']))


def dashboard():
    states, rows = [], []
    now = datetime.now(timezone.utc)
    for machine in MACHINES:
        transport, pulls = {}, STORE / (machine + '-pull.json')
        try:
            snapshot = read(machine)
            if pulls.exists():
                transport = json.loads(pulls.read_text())
        except (OSError, ValueError, KeyError, TypeError):
            states.append(dict(machineId=machine, state='error', dataUpdatedAt=None))
            continue
        if snapshot is None:
            states.append(dict(machineId=machine, state='error' if transport.get('status') == 'error' else 'pending',
                               dataUpdatedAt=None, transport=transport))
            continue
        age = (now - stamp(snapshot['dataUpdatedAt'])).total_seconds() if snapshot['dataUpdatedAt'] else None
        # A pulled snapshot often lags successful pulls by a few minutes; only a longer gap suggests its export stopped.
        limit = 900 if transport.get('status') == 'ok' else 180
        state = 'error' if snapshot['status'] == 'error' else 'stale' if age is None or age > limit else 'fresh'
        if transport.get('status') == 'error':
            state = 'error'
        states.append(dict(machineId=machine, state=state, dataUpdatedAt=snapshot['dataUpdatedAt'],
                           coverage=snapshot['coverage'], transport=transport))
        rows.extend(snapshot['rows'])
    return dict(schemaVersion=1, generatedAt=utcnow(), timezone=ZONE, machines=states, rows=rows)


if __name__ == '__main__':
    capture()
