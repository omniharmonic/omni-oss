import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import io
from contextlib import redirect_stdout
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from proactive_intelligence.intake import Intake, coordinator_lock, Backoff, safe_to_ack, acknowledge_if_safe
from proactive_intelligence.state import State
from proactive_intelligence.runner import Runner
from test_install_proactive import FakeAPI
from install_proactive import install, wrapper
from datetime import datetime
from zoneinfo import ZoneInfo
NOW=int(datetime(2026,10,12,12,tzinfo=ZoneInfo('America/Edmonton')).timestamp()*1000)
def lock_child(path,pipe):
    with coordinator_lock(Path(path)) as acquired:pipe.send(acquired)
class Tests(unittest.TestCase):
    def test_generation_debounce_daytime_and_exact_ack(self):
        calls=[]
        def fetch(path,body):
            calls.append((path,body));return {'generation':3,'pending':True,'readyAt':NOW+1} if not path else {'acknowledged':False}
        intake=Intake('http://127.0.0.1:8787','test',fetch)
        self.assertIsNone(intake.ready(NOW));self.assertEqual(intake.ready(NOW+1),3)
        intake.acknowledge(3);self.assertEqual(calls[-1],('/ack',{'generation':3}))
        night=int(datetime(2026,10,12,22,tzinfo=ZoneInfo('America/Edmonton')).timestamp()*1000)
        n=len(calls);self.assertIsNone(intake.ready(night));self.assertEqual(len(calls),n)
    def test_restart_backoff_and_all_coordinators_share_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.db';state=State(path);backoff=Backoff(state)
            self.assertTrue(backoff.begin(NOW));state.db.close()
            state=State(path);self.assertFalse(Backoff(state).begin(NOW+60000));self.assertTrue(Backoff(state).begin(NOW+900000));state.db.close()
            with coordinator_lock(path) as acquired:
                self.assertTrue(acquired)
                ctx=multiprocessing.get_context('spawn');reader,writer=ctx.Pipe();p=ctx.Process(target=lock_child,args=(str(path),writer));p.start()
                self.assertFalse(reader.recv());p.join(10);self.assertEqual(p.exitcode,0)
            with coordinator_lock(path) as acquired:self.assertTrue(acquired)
    def test_duplicate_restart_reconcile_spends_zero_additional_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.db';state=State(path);calls=[]
            class Reasoner:
                def reason(self,packet):
                    calls.append(packet)
                    return {'decisions':[{'sourceId':'a','sourceRevision':'r1','action':'ignore','title':'No work','summary':'No work required','body':'No work required','evidence':[{'sourceId':'a','revision':'r1'}]}]}
            event={'id':'a','revision':'r1','entity':'a','kind':'task','occurred_at':NOW,'content':'synthetic','url':'prism://note/a'}
            state.ingest('vault','1',[event],now=NOW,coalesce_ms=0)
            Runner(state,Reasoner(),clock=lambda:NOW).tick();state.db.close()
            state=State(path)
            for i in range(10):state.ingest('vault','1',[event],now=NOW,coalesce_ms=0)
            Runner(state,Reasoner(),clock=lambda:NOW).tick();self.assertEqual(len(calls),1);self.assertTrue(safe_to_ack(state))
            state.db.close()
    def test_no_event_cli_does_not_read_sources_or_construct_reasoner(self):
        import proactive_intelligence.__main__ as cli
        with tempfile.TemporaryDirectory() as directory:
            args=['proactive','--state',str(Path(directory)/'state.db'),'--event-intake','--reconcile','--reason','--live']
            with patch.object(sys,'argv',args), patch('proactive_intelligence.intake.Intake.ready',return_value=None), patch.object(cli,'State') as state, patch.object(cli,'HermesReasoner') as reasoner, redirect_stdout(io.StringIO()) as output:
                cli.main();state.assert_not_called();reasoner.assert_not_called()
                self.assertEqual(json.loads(output.getvalue())['mode'],'event-idle')
    def test_cli_busy_skips_all_work_then_releases(self):
        import proactive_intelligence.__main__ as cli
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.db'
            with patch.object(sys,'argv',['proactive','--state',str(path)]),patch.object(cli,'_main') as work,redirect_stdout(io.StringIO()):
                with coordinator_lock(path):cli.main();work.assert_not_called()
                cli.main();work.assert_called_once()
    def test_unsafe_outcomes_keep_signal_and_generation_race_keeps_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            state=State(Path(directory)/'state.db');backoff=Backoff(state);self.assertTrue(backoff.begin(NOW))
            intake=Intake('http://127.0.0.1:8787','test',lambda path,body:{'acknowledged':False})
            self.assertFalse(acknowledge_if_safe(intake,2,state,backoff))
            self.assertFalse(backoff.begin(NOW+60000))
            state.db.execute("INSERT INTO outbox(id,payload,status) VALUES('unknown','{}','unknown')")
            self.assertFalse(safe_to_ack(state))
            with patch.object(intake,'acknowledge') as ack:
                self.assertFalse(acknowledge_if_safe(intake,2,state,backoff));ack.assert_not_called()
            state.db.execute('DELETE FROM outbox')
            state.ingest('vault',None,[],health='unavailable',now=NOW)
            self.assertFalse(safe_to_ack(state));state.ingest('vault','ok',[],health='healthy',now=NOW)
            self.assertTrue(safe_to_ack(state));state.db.close()
    def test_loopback_only_no_proxy_no_redirect(self):
        for url in ['https://127.0.0.1:8787','http://example.com','http://user:secret@localhost','http://localhost/path','http://localhost?query=1','http://localhost#fragment']:
            with self.assertRaises(ValueError):Intake(url,'SECRET')
        import urllib.request
        intake=Intake('http://127.0.0.1:8787','test')
        proxy=next((h for h in intake.opener.handlers if isinstance(h,urllib.request.ProxyHandler)),None)
        self.assertTrue(proxy is None or proxy.proxies=={})
        redirect=next(h for h in intake.opener.handlers if isinstance(h,urllib.request.HTTPRedirectHandler))
        with self.assertRaises(ValueError):redirect.redirect_request(None,None,302,'',{},'http://external.test')
    def test_events_install_paused_uses_same_state_role_and_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);api=FakeAPI();result=install(api,root,root/'home',root/'receipt',True,routine='events')
            self.assertFalse(result['job']['enabled']);self.assertEqual(result['job']['schedule'],'* 8-20 * * *')
            source=(root/'home/scripts/omni-proactive-events.py').read_text()
            self.assertIn('--event-intake',source);self.assertIn('--reconcile',source);self.assertIn(str(root/'state/proactive.sqlite3'),source)
            self.assertNotIn("'--routine', 'events'",source)
if __name__=='__main__':unittest.main()
