"""Aggregate ccusage snapshots only; no transcript or SQLite parsing."""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Runtime data stays in the config directory so the code can live in a Git checkout.
STORE = Path.home() / '.config/cliproxyapi/dashboard/ccusage-snapshots'
MACHINES = ('aleks-mbp', 'genius')
ZONE = 'America/Mexico_City'
COMMAND = ['daily', '--json', '--by-agent', '--offline', '--no-cost', '--timezone', ZONE]
COUNTERS = ('inputTokens', 'cacheCreationTokens', 'cacheReadTokens', 'outputTokens', 'totalTokens')
SEMANTICS = 'input_excludes_cache; total=input+cacheCreation+cacheRead+output'
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
    return dict(schemaVersion=1, machine={'id': machine}, timezone=ZONE,
                generatedAt=snapshot['generatedAt'], dataUpdatedAt=updated, status=snapshot['status'],
                source=dict(tool='ccusage', version=version, offline=True, costsIncluded=False, command=COMMAND),
                coverage=dict(apps=sorted({r['app'] for r in rows}), scope='ccusage_reported_local_history',
                              historyPolicy='replace_app_day_models_preserve_absent_days', counterSemantics=SEMANTICS,
                              reportedRows=sum(r['reportedInLatest'] for r in rows),
                              retainedHistoricalRows=sum(not r['reportedInLatest'] for r in rows),
                              limitations=LIMITATIONS + (['Genius ccusage 20.0.20 has known modern-event coverage gaps.']
                                                         if machine == 'genius' and version == '20.0.20' else [])),
                rows=rows, errors=errors)


def read(machine):
    path = STORE / (machine + '.json')
    return validate(json.loads(path.read_text())) if path.exists() else None


def save(snapshot):
    snapshot = validate(snapshot)
    previous = read(snapshot['machine']['id'])
    if previous and stamp(snapshot['generatedAt']) < stamp(previous['generatedAt']):
        raise ValueError('Older snapshot cannot replace newer data')
    replaced = {(r['app'], r['date']) for r in snapshot['rows'] if r['reportedInLatest']}
    incoming = {(r['app'], r['date'], r['model']): r for r in snapshot['rows']}
    for row in (previous or {}).get('rows', []):
        key = (row['app'], row['date'], row['model'])
        if (row['app'], row['date']) not in replaced and key not in incoming:
            incoming[key] = dict(row, reportedInLatest=False)
    snapshot['rows'] = sorted(incoming.values(), key=lambda r: (r['date'], r['app'], r['model'] or ''))
    snapshot['coverage'].update(reportedRows=sum(r['reportedInLatest'] for r in snapshot['rows']),
                                retainedHistoricalRows=sum(not r['reportedInLatest'] for r in snapshot['rows']))
    STORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=STORE, delete=False) as handle:
            temp = Path(handle.name)
            json.dump(snapshot, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, STORE / (snapshot['machine']['id'] + '.json'))
    finally:
        if temp and temp.exists():
            temp.unlink()
    return snapshot


def capture():
    now = utcnow()
    previous = read('aleks-mbp')
    try:
        result = subprocess.run(['/opt/homebrew/bin/ccusage', *COMMAND], capture_output=True, text=True,
                                timeout=55, check=True)
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
                if sum(r['totalTokens'] for r in rows if r['app'] == app['agent'] and r['date'] == day['period']) != app['totalTokens']:
                    raise ValueError('Model breakdown does not match ccusage total')
        snapshot = dict(schemaVersion=1, machine={'id': 'aleks-mbp'}, timezone=ZONE, generatedAt=now,
                        dataUpdatedAt=now, status='ok', source=dict(tool='ccusage', version=version,
                        offline=True, costsIncluded=False), coverage=dict(counterSemantics=SEMANTICS), rows=rows, errors=[])
        return save(snapshot)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        snapshot = previous or dict(schemaVersion=1, machine={'id': 'aleks-mbp'}, timezone=ZONE,
                        dataUpdatedAt=None, source=dict(tool='ccusage', version='unknown', offline=True,
                        costsIncluded=False), coverage=dict(counterSemantics=SEMANTICS), rows=[])
        snapshot.update(generatedAt=now, status='error', errors=[dict(source='ccusage', code='capture_failed', at=now)])
        snapshot['rows'] = [dict(row, reportedInLatest=False) for row in snapshot['rows']]
        return save(snapshot)


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
        snapshot = validate(json.loads(raw))
        if snapshot['machine']['id'] != 'genius':
            raise ValueError('Unexpected snapshot machine')
        save(snapshot)
        health.update(status='ok', lastSuccessAt=utcnow(), successfulPulls=health['successfulPulls'] + 1,
                      snapshotGeneratedAt=snapshot['generatedAt'], httpStatus=200)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        health['error'] = 'snapshot_pull_failed'
    STORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=STORE, delete=False) as handle:
        temp = Path(handle.name)
        json.dump(health, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp, health_file)
    finally:
        if temp.exists():
            temp.unlink()
    return health


def collect(action=capture):
    while True:
        start = time.monotonic()
        try:
            action()
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(max(1, 60 - (time.monotonic() - start)))


def dashboard():
    states, rows = [], []
    now = datetime.now(timezone.utc)
    for machine in MACHINES:
        transport = {}
        try:
            snapshot = read(machine)
            if machine == 'genius' and (STORE / 'genius-pull.json').exists():
                transport = json.loads((STORE / 'genius-pull.json').read_text())
        except (OSError, ValueError, KeyError, TypeError):
            snapshot = None
            states.append(dict(machineId=machine, state='error', generatedAt=None, dataUpdatedAt=None))
            continue
        if snapshot is None:
            states.append(dict(machineId=machine, state='error' if transport.get('status') == 'error' else 'pending',
                               generatedAt=None, dataUpdatedAt=None, transport=transport))
            continue
        age = (now - stamp(snapshot['dataUpdatedAt'])).total_seconds() if snapshot['dataUpdatedAt'] else None
        state = 'error' if snapshot['status'] == 'error' else 'stale' if age is None or age > 180 else 'fresh'
        if transport.get('status') == 'error':
            state = 'error'
        states.append(dict(machineId=machine, state=state, generatedAt=snapshot['generatedAt'],
                           dataUpdatedAt=snapshot['dataUpdatedAt'], coverage=snapshot['coverage'], source=snapshot['source'],
                           transport=transport))
        rows.extend(snapshot['rows'])
    return dict(schemaVersion=1, generatedAt=utcnow(), timezone=ZONE, machines=states, rows=rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Capture local ccusage or import a Genius aggregate snapshot')
    parser.add_argument('--import-snapshot', type=Path)
    args = parser.parse_args()
    if args.import_snapshot:
        if args.import_snapshot.stat().st_size > 4 * 1024 * 1024:
            parser.error('Snapshot exceeds four MiB')
        snapshot = validate(json.loads(args.import_snapshot.read_text()))
        if snapshot['machine']['id'] != 'genius':
            parser.error('Only Genius imports are accepted; local data comes from ccusage')
        save(snapshot)
    else:
        capture()
