import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import ccusage_data as c
import io

# User contract: ccusage-only capture, aggregate replacement, privacy, real pending/stale states.
def fixture(machine='genius', when=None):
    now = when or c.utcnow()
    return dict(schemaVersion=1, machine={'id':machine,'hostname':'fixture'}, timezone=c.ZONE,
        generatedAt=now, dataUpdatedAt=now, status='ok', source=dict(tool='ccusage',version='20.0.20',offline=True,costsIncluded=False),
        coverage=dict(counterSemantics=c.SEMANTICS), errors=[], rows=[dict(machineId=machine,app='codex',date='2026-10-11',model='gpt-test',inputTokens=10,
            cacheReadTokens=20,cacheCreationTokens=0,outputTokens=3,totalTokens=33,lastReportedAt=now,reportedInLatest=True)])

class Response(io.BytesIO):
    status=200
    def geturl(self):return c.GENIUS_URL

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.patch=patch.object(c,'STORE',Path(self.temp.name)/'snapshots');self.patch.start();self.addCleanup(self.patch.stop)

    def test_repeated_replacement_models_and_history(self):
        initial=fixture();old=copy.deepcopy(initial['rows'][0]);old['date']='2026-10-10';initial['rows'].append(old)
        c.save(initial);c.save(initial)
        self.assertEqual(sum(r['totalTokens'] for r in c.dashboard()['rows']),66)
        new=fixture();new['rows'][0].update(model='gpt-new',inputTokens=11,totalTokens=34)
        c.save(new);rows=c.dashboard()['rows']
        self.assertEqual({(r['date'],r['model']) for r in rows},{('2026-10-10','gpt-test'),('2026-10-11','gpt-new')})
        self.assertEqual(sum(r['totalTokens'] for r in rows),67)
        self.assertFalse(rows[0]['reportedInLatest'])

    def test_boundaries_reject_without_overwriting(self):
        good=fixture();c.save(good);original=(c.STORE/'genius.json').read_bytes()
        for mutate in (lambda x:x['rows'][0].update(inputTokens=-1),lambda x:x['rows'][0].update(totalTokens=999),
                       lambda x:x['rows'][0].update(machineId='aleks-mbp'),lambda x:x['rows'].append(x['rows'][0]),
                       lambda x:x.update(generatedAt='2000-01-01T00:00:00Z',dataUpdatedAt=None,rows=[])):
            bad=copy.deepcopy(good);mutate(bad)
            with self.assertRaises(ValueError):c.save(bad)
            self.assertEqual((c.STORE/'genius.json').read_bytes(),original)

    def test_privacy_pending_stale_error(self):
        self.assertEqual([m['state'] for m in c.dashboard()['machines']],['pending','pending'])
        old=(datetime.now(timezone.utc)-timedelta(seconds=181)).isoformat();data=fixture(when=old)
        data['prompt']='PRIVATE';data['source']['token']='SECRET';data['rows'][0]['project']='/private/project'
        c.save(data);saved=(c.STORE/'genius.json').read_text()
        self.assertNotIn('PRIVATE',saved);self.assertNotIn('SECRET',saved);self.assertNotIn('/private/project',saved)
        self.assertEqual(c.dashboard()['machines'][1]['state'],'stale')
        error=fixture();error.update(status='error',dataUpdatedAt=old,rows=[])
        error['errors']=[dict(source='SECRET',code='PRIVATE',at=c.utcnow())]
        c.save(error)
        self.assertEqual(c.dashboard()['machines'][1]['state'],'error')
        self.assertEqual(c.dashboard()['rows'][0]['totalTokens'],33)
        self.assertNotIn('SECRET',(c.STORE/'genius.json').read_text())

    def test_local_capture_only_ccusage_and_failure_preserves(self):
        report={'daily':[{'period':'2026-10-11','agents':[dict(agent='claude',inputTokens=1,cacheReadTokens=2,cacheCreationTokens=3,outputTokens=4,totalTokens=10,modelBreakdowns=[])]}]}
        results=[subprocess.CompletedProcess([],0,json.dumps(report),''),subprocess.CompletedProcess([],0,'20.0.26\n','')]
        with patch.object(c.subprocess,'run',side_effect=results) as run:
            result=c.capture()
            self.assertEqual(result['rows'][0]['totalTokens'],10)
            self.assertIsNone(result['rows'][0]['model'])
            self.assertTrue({'--offline','--no-cost'} <= set(run.call_args_list[0].args[0]))
        with patch.object(c.subprocess,'run',side_effect=subprocess.CalledProcessError(1,[],stderr='SECRET PROMPT')):
            result=c.capture()
        self.assertEqual(result['status'],'error');self.assertEqual(result['rows'][0]['totalTokens'],10)
        self.assertNotIn('SECRET',(c.STORE/'aleks-mbp.json').read_text())

    def test_partial_capture_keeps_older_days_reported(self):
        # Aleks: "go with --since, and keep older days from looking stale".
        today=datetime.now(c.ZoneInfo(c.ZONE)).date();old=str(today-timedelta(days=5));recent=str(today)
        def report(*days):
            day=lambda d:{'period':d,'agents':[dict(agent='claude',inputTokens=1,cacheReadTokens=0,cacheCreationTokens=0,outputTokens=1,totalTokens=2,modelBreakdowns=[])]}
            return [subprocess.CompletedProcess([],0,json.dumps({'daily':[day(d) for d in days]}),''),subprocess.CompletedProcess([],0,'20.0.26\n','')]
        with patch.object(c.subprocess,'run',side_effect=report(old,recent)):c.capture()
        with patch.object(c.subprocess,'run',side_effect=report(recent)):rows=c.capture()['rows']
        self.assertEqual({r['date']:r['reportedInLatest'] for r in rows},{old:True,recent:True})

    def test_https_pull_idempotent_failure_and_recovery(self):
        data=fixture()
        opener=unittest.mock.Mock()
        opener.open.side_effect=lambda *a,**k:Response(json.dumps(data).encode())
        with patch.object(c,'build_opener',return_value=opener):
            first=c.pull_genius();second=c.pull_genius()
        self.assertEqual(first['successfulPulls'],1);self.assertEqual(second['successfulPulls'],2)
        saved=(c.STORE/'genius.json').read_bytes()
        self.assertEqual(sum(r['totalTokens'] for r in c.dashboard()['rows']),33)
        opener.open.side_effect=OSError('PRIVATE NETWORK DETAILS')
        with patch.object(c,'build_opener',return_value=opener):c.pull_genius()
        self.assertEqual((c.STORE/'genius.json').read_bytes(),saved)
        self.assertEqual(c.dashboard()['machines'][1]['state'],'error')
        self.assertEqual(c.dashboard()['rows'][0]['totalTokens'],33)
        self.assertNotIn('PRIVATE',(c.STORE/'genius-pull.json').read_text())
        opener.open.side_effect=lambda *a,**k:Response(json.dumps(data).encode())
        with patch.object(c,'build_opener',return_value=opener):c.pull_genius()
        self.assertEqual(c.dashboard()['machines'][1]['state'],'fresh')

    def test_https_rejects_redirect_size_origin_schema_and_machine(self):
        data=fixture();c.save(data);saved=(c.STORE/'genius.json').read_bytes()
        with self.assertRaises(ValueError):c.NoRedirect().redirect_request(None,None,None,None,None,None)
        wrong=fixture('aleks-mbp');bad_schema=fixture();bad_schema['schemaVersion']=True
        wrong_origin=Response(json.dumps(data).encode());wrong_origin.geturl=lambda:'https://unexpected.example/snapshot.json'
        for result in [Response(b'x'*(c.MAX_SNAPSHOT_BYTES+1)),wrong_origin,
                       Response(json.dumps(wrong).encode()),Response(json.dumps(bad_schema).encode())]:
            opener=unittest.mock.Mock();opener.open.return_value=result
            with patch.object(c,'build_opener',return_value=opener):self.assertEqual(c.pull_genius()['status'],'error')
            self.assertEqual((c.STORE/'genius.json').read_bytes(),saved)

    def test_first_network_failure_is_error_without_fake_data(self):
        opener=unittest.mock.Mock();opener.open.side_effect=OSError('unreachable')
        with patch.object(c,'build_opener',return_value=opener):c.pull_genius()
        self.assertEqual(c.dashboard()['machines'][1]['state'],'error')
        self.assertIsNone(c.dashboard()['machines'][1]['dataUpdatedAt'])
        self.assertEqual(c.dashboard()['rows'],[])

if __name__=='__main__':unittest.main()
