import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from proactive_intelligence.source_health import SourceHealthGuard, EXPECTED_BRIDGES, fresh_bridge_health
from datetime import datetime,timezone
NOW=1791800000000
iso=lambda n:datetime.fromtimestamp(n/1000,timezone.utc).isoformat()
def worker():return {'checkedAt':iso(NOW),'sources':[{'name':'matrix','status':'ok','kind':'server','lastSuccessAt':iso(NOW-1000),'staleAfterMs':60000,'failureStreak':0,'reconcileOwed':0,'reconcileStuck':0}]}
def bridge():return {'checkedAt':iso(NOW),'synapse':'healthy','auth':'valid','bridges':{name:'connected' for name in EXPECTED_BRIDGES}}
class Tests(unittest.TestCase):
    def test_missing_bridge_fetch_fails_closed_by_default(self):
        guard=SourceHealthGuard(fetch=worker,clock=lambda:NOW)
        self.assertFalse(guard.refresh()['healthy']);self.assertIn('bridge',guard.warning())
    def test_worker_and_bridges_required_other_channels_continue(self):
        receipt=bridge();receipt['bridges']['telegrambot']='failed'
        guard=SourceHealthGuard(fetch=worker,fetch_bridge=lambda:receipt,clock=lambda:NOW)
        result=guard.refresh();self.assertFalse(result['healthy']);self.assertEqual(result['reason'],'bridge_coverage_unverified')
        events=[{'channel':'message-thread'},{'channel':'email'},{'channel':'task'}]
        self.assertEqual(guard.filter(events)['eligible'],events[1:])
        receipt['bridges']['telegrambot']='connected';self.assertTrue(guard.refresh()['healthy'])
    def test_partial_stale_future_missing_auth_and_bad_states_hold(self):
        cases=[None,{},dict(bridge(),checkedAt=iso(NOW-300001)),dict(bridge(),checkedAt=iso(NOW+1)),dict(bridge(),auth='failed'),dict(bridge(),synapse='unreachable')]
        missing=bridge();del missing['bridges']['twitterbot'];cases.append(missing)
        for status in ['degraded','failed','down','stale','unknown','not deployed']:
            value=bridge();value['bridges']['whatsappbot']=status;cases.append(value)
        for value in cases:
            with self.subTest(value=value):self.assertFalse(SourceHealthGuard(fetch=worker,fetch_bridge=lambda:value,clock=lambda:NOW).refresh()['healthy'])
    def test_probe_failure_does_not_raise_or_use_old_heartbeat(self):
        def run(*args,**kwargs):raise subprocess.TimeoutExpired(args[0],15)
        self.assertIsNone(fresh_bridge_health(run=run,clock=lambda:NOW))
        guard=SourceHealthGuard(fetch=worker,fetch_bridge=lambda:fresh_bridge_health(run=run),clock=lambda:NOW)
        self.assertFalse(guard.refresh()['healthy'])
    def test_exact_trusted_probe_bounded_and_result_is_state_only(self):
        calls=[];raw=bridge();raw.update(authenticated_as='PRIVATE ID',homeserver='PRIVATE URL',bridge_detail={'telegrambot':'SECRET'},joined_rooms=900)
        raw['bridges']['signalbot']='not deployed'
        def run(args,**kwargs):calls.append((args,kwargs));return SimpleNamespace(returncode=0,stdout='MATRIX HEALTH CHECK\n--- JSON ---\n'+json.dumps(raw))
        value=fresh_bridge_health(run=run,clock=lambda:NOW)
        self.assertEqual(set(value),{'checkedAt','synapse','auth','bridges'});self.assertNotIn('PRIVATE',json.dumps(value));self.assertNotIn('SECRET',json.dumps(value));self.assertNotIn('signalbot',value['bridges'])
        args,kwargs=calls[0];self.assertEqual(args[0],sys.executable);self.assertTrue(args[1].endswith('/scripts/matrix_reader.py'));self.assertEqual(args[2:],["health"]);self.assertEqual(kwargs['timeout'],15);self.assertFalse(kwargs['check']);self.assertNotIn('shell',kwargs)
    def test_malformed_failed_oversized_and_partial_probe_hold(self):
        for result in [SimpleNamespace(returncode=1,stdout='--- JSON ---\n{}'),SimpleNamespace(returncode=0,stdout='bad'),SimpleNamespace(returncode=0,stdout='x'*32769),SimpleNamespace(returncode=0,stdout='--- JSON ---\n{'),SimpleNamespace(returncode=0,stdout='--- JSON ---\n'+json.dumps({'bridges':{}}))]:
            self.assertIsNone(fresh_bridge_health(run=lambda *a,**k:result,clock=lambda:NOW))
    def test_failed_worker_does_not_become_healthy_from_good_bridges(self):
        value=worker();value['sources'][0]['status']='failing'
        guard=SourceHealthGuard(fetch=lambda:value,fetch_bridge=bridge,clock=lambda:NOW)
        self.assertFalse(guard.refresh()['healthy'])
if __name__=='__main__':unittest.main()
