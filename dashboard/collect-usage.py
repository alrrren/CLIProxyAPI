#!/opt/homebrew/bin/python3
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import runpy
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = Path.home() / '.config/cliproxyapi'
FIELDS = ('timestamp', 'latency_ms', 'source', 'auth_index', 'failed',
          'provider', 'model', 'alias', 'endpoint', 'auth_type', 'request_id', 'ttft_ms', 'stream')
TOKENS = ('input_tokens', 'output_tokens', 'reasoning_tokens', 'cached_tokens', 'total_tokens',
          'cache_read_tokens', 'cache_creation_tokens', 'cache_read_tokens_present')


def clean(record):
    result = {key: record[key] for key in FIELDS if key in record}
    result['tokens'] = {key: record['tokens'][key] for key in TOKENS if key in record.get('tokens', {})}
    result['status_code'] = record.get('fail', {}).get('status_code')
    # Tailscale serve forwards the caller's tailnet address, which tells Genius traffic apart from this Mac's.
    result['client'] = record.get('x_forwarded_for', '').split(',')[0].strip() or record.get('client_ip') or None
    breakdown = record.get('token_breakdown', {})
    if breakdown:
        result['token_breakdown'] = {
            key: breakdown[key] for key in ('schema_version', 'quality', 'total_tokens', 'unclassified_tokens') if key in breakdown}
        for side, names in [('input', ('total_tokens', 'uncached_tokens', 'cache_read_tokens', 'cache_write_tokens')),
                            ('output', ('total_tokens', 'non_reasoning_tokens', 'reasoning_tokens'))]:
            result['token_breakdown'][side] = {key: breakdown[side][key] for key in names if key in breakdown.get(side, {})}
    return result


def main():
    os.umask(0o077)
    with (ROOT / 'usage-collector.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with sqlite3.connect(ROOT / 'usage.sqlite3') as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, record TEXT NOT NULL)')
            if '--summary' in sys.argv:
                rows = db.execute("SELECT substr(json_extract(record, '$.timestamp'),1,10), json_extract(record, '$.source'), json_extract(record, '$.model'), count(*), sum(json_extract(record, '$.tokens.input_tokens')), sum(json_extract(record, '$.tokens.output_tokens')), sum(json_extract(record, '$.tokens.cached_tokens')) FROM requests GROUP BY 1,2,3 ORDER BY 1 DESC")
                for row in rows:
                    print(json.dumps(dict(zip(('date', 'account', 'model', 'requests', 'input_tokens', 'output_tokens', 'cached_tokens'), row))))
                return
            keys = json.loads((ROOT / 'connection-keys.json').read_text())
            total = 0
            for _ in range(20):
                request = urllib.request.Request(
                    'http://127.0.0.1:8317/v8/management/observability/usage/queue?count=1000',
                    headers={'Authorization': 'Bearer ' + keys['management_key']})
                with urllib.request.urlopen(request, timeout=10) as response:
                    records = json.load(response)
                assert isinstance(records, list), 'Unexpected usage response'
                if not records:
                    break
                with db:
                    db.executemany('INSERT INTO requests(record) VALUES (?)',
                                   [(json.dumps(clean(record)),) for record in records])
                total += len(records)
            if total:
                print(f'Saved {total} usage records', flush=True)
            temp = ROOT / 'collector-status.tmp'
            temp.write_text(json.dumps({'last_success': datetime.now(timezone.utc).isoformat(), 'saved': total}))
            os.replace(temp, ROOT / 'collector-status.json')
            try:
                runpy.run_path(str(ROOT / 'account-pool.py'))['update']()
            except Exception as error:
                print(f'Account pool update failed: {type(error).__name__}', file=sys.stderr)


if __name__ == '__main__':
    if '--self-test' in sys.argv:
        sample = {'timestamp': '2026-10-11T01:00:00Z', 'api_key': 'SECRET',
                  'prompt': 'PRIVATE', 'tokens': {'input_tokens': 12, 'api_key': 'SECRET'}}
        filtered = clean(sample)
        assert filtered['tokens']['input_tokens'] == 12
        assert 'SECRET' not in json.dumps(filtered) and 'PRIVATE' not in json.dumps(filtered)
        print('Secret filtering self-test passed')
    else:
        try:
            main()
        except Exception as error:
            print(f'Usage collection failed: {type(error).__name__}', file=sys.stderr)
            sys.exit(1)
