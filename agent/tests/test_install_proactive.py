import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from install_proactive import install,rollback,NAME,ROUTINE_SCHEDULES,NativeJobs

class FakeAPI:
    def __init__(self):self.jobs=[];self.calls=[]
    def call(self,method,path,body=None):
        self.calls.append((method,path,body))
        if method=='GET':return {'jobs':self.jobs.copy()}
        if path.endswith('/pause'):
            self.jobs[0]['enabled']=False;self.jobs[0]['state']='paused';return {'job':self.jobs[0]}
        if path.endswith('/resume'):
            self.jobs[0]['enabled']=True;self.jobs[0]['state']='scheduled';return {'job':self.jobs[0]}
        if method=='POST':self.jobs.append(dict(body,id='abc123'))
        if method=='PATCH':
            # Real Hermes can report enabled=true from an update while durable
            # lifecycle state remains paused. Only resume changes both fields.
            was_paused=self.jobs[0].get('state')=='paused'
            self.jobs[0].update(body)
            if was_paused and body.get('enabled'):
                self.jobs[0]['enabled']=False
                return {'job':dict(self.jobs[0],enabled=True)}
        return {'job':self.jobs[0].copy()}

class InstallerTests(unittest.TestCase):
    def test_dryrun_apply_update_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);home=root/'hermes';api=FakeAPI()
            install(api,root,home,root/'receipt');self.assertFalse(home.exists());self.assertEqual(len(api.jobs),0)
            first=install(api,root,home,root/'receipt',True)
            self.assertEqual(first['job_id'],'abc123');self.assertEqual(len(api.jobs),1)
            self.assertTrue((home/'scripts/omni-proactive-hourly.py').exists())
            install(api,root,home,root/'update',True);self.assertEqual(len(api.jobs),1)
            rollback(api,root/'receipt');self.assertTrue(api.jobs[0]['enabled'])
            rollback(api,root/'receipt',True);self.assertFalse(api.jobs[0]['enabled'])
            self.assertFalse((home/'scripts/omni-proactive-hourly.py').exists())
    def test_health_job_recipe_and_wrapper(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);home=root/'hermes';api=FakeAPI()
            plan=install(api,root,home,root/'dry',health=True)
            self.assertFalse(home.exists());self.assertEqual(api.jobs,[])
            self.assertEqual(plan['job']['name'],'omni-health');self.assertEqual(plan['job']['schedule'],'0 21 * * *')
            self.assertEqual(plan['timezone'],'America/Edmonton')
            self.assertTrue(plan['job']['no_agent']);self.assertEqual(plan['job']['deliver'],'local')
            install(api,root,home,root/'receipt',True,health=True)
            self.assertEqual(len(api.jobs),1);self.assertTrue(api.jobs[0]['enabled'])
            src=(home/'scripts/omni-health.py').read_text()
            self.assertIn('omni_health.py',src);self.assertIn('proactive.sqlite3',src)
            compile(src,'w','exec')
            with self.assertRaises(ValueError):install(api,root,home,root/'x',routine='morning',health=True)
    def test_memory_sync_job_recipe_and_wrapper(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);home=root/'hermes';api=FakeAPI()
            plan=install(api,root,home,root/'dry',memory_sync=True)
            self.assertFalse(home.exists());self.assertEqual(api.jobs,[])
            self.assertEqual(plan['job']['name'],'omni-hermes-memory-sync');self.assertEqual(plan['job']['schedule'],'17 * * * *')
            self.assertTrue(plan['job']['no_agent']);self.assertEqual(plan['job']['deliver'],'local');self.assertNotIn('model',plan['job'])
            install(api,root,home,root/'receipt',True,memory_sync=True)
            self.assertEqual(len(api.jobs),1);self.assertTrue(api.jobs[0]['enabled'])
            src=(home/'scripts/omni-hermes-memory-sync.py').read_text()
            self.assertIn('hermes_memory_sync.py',src);self.assertIn('hermes-memory-sync.json',src);self.assertIn("'--hermes-home'",src)
            compile(src,'w','exec')
            # Only the Parachute variables, from the repo .env, reach the sync: no wallet key, no setdefault.
            self.assertIn('os.execve(',src);self.assertNotIn('setdefault',src);self.assertNotIn("home/'.env'",src)
            self.assertIn("('PARACHUTE_URL', 'PARACHUTE_VAULT', 'PARACHUTE_API_KEY')",src)
            install(api,root,home,root/'again',True,memory_sync=True);self.assertEqual(len(api.jobs),1)
            for bad in (dict(routine='morning'),dict(health=True),dict(model='m')):
                with self.assertRaises(ValueError):install(api,root,home,root/'x',memory_sync=True,**bad)
    def test_routine_paused_then_enabled_exact_name(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);home=root/'hermes';api=FakeAPI()
            result=install(api,root,home,root/'paused',True,routine='morning')
            self.assertFalse(result['job']['enabled'])
            self.assertEqual(api.jobs[0]['name'],'omni-proactive-morning')
            self.assertIn("'--routine', 'morning'",(home/'scripts/omni-proactive-morning.py').read_text())
            self.assertEqual((root/'paused/jobs-before.json').stat().st_mode&0o777,0o600)
            result=install(api,root,home,root/'enabled',True,routine='morning',enable=True)
            self.assertTrue(result['job']['enabled']);self.assertEqual(len(api.jobs),1)
            self.assertEqual(api.jobs[0]['state'],'scheduled')
            self.assertTrue(any(path.endswith('/resume') for _,path,_ in api.calls))
            self.assertEqual(api.calls[-1][0],'GET')
            rollback(api,root/'enabled',True);self.assertFalse(api.jobs[0]['enabled'])

    def test_rollback_explicitly_restores_and_verifies_paused_lifecycle(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);api=FakeAPI()
            install(api,root,root/'hermes',root/'paused',True,paused=True)
            install(api,root,root/'hermes',root/'enabled',True)
            start=len(api.calls)
            rollback(api,root/'enabled',True)
            calls=api.calls[start:]
            patch=next(i for i,(method,_,_) in enumerate(calls) if method=='PATCH')
            self.assertTrue(any(path.endswith('/pause') for _,path,_ in calls[patch+1:]))
            self.assertEqual(calls[-1][0],'GET')
            self.assertFalse(api.jobs[0]['enabled']);self.assertEqual(api.jobs[0]['state'],'paused')

    def test_resume_unconfirmed_fails_closed(self):
        class RefusesResume(FakeAPI):
            def call(self,method,path,body=None):
                if path.endswith('/resume'):
                    return {'job':dict(self.jobs[0],enabled=True)}
                return super().call(method,path,body)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);api=RefusesResume()
            install(api,root,root/'hermes',root/'paused',True,paused=True)
            with self.assertRaises(ValueError):
                install(api,root,root/'hermes',root/'activation',True)
            self.assertFalse(api.jobs[0]['enabled']);self.assertEqual(api.jobs[0]['state'],'paused')

    def test_nightly_review_is_later_than_cloud_weave_and_paused(self):
        self.assertEqual(ROUTINE_SCHEDULES['nightly-review'],'30 5 * * *')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);result=install(FakeAPI(),root,root/'hermes',root/'receipt',routine='nightly-review')
            self.assertFalse(result['job']['enabled'])

    def test_hourly_explicit_paused_and_conflicting_flags(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);api=FakeAPI()
            result=install(api,root,root/'hermes',root/'paused',True,paused=True)
            self.assertFalse(result['job']['enabled']);self.assertFalse(api.jobs[0]['enabled'])
            with self.assertRaises(ValueError):
                install(api,root,root/'hermes',root/'invalid',paused=True,enable=True)
            import subprocess
            run=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/install_proactive.py'),'--paused','--enable'],capture_output=True,text=True)
            self.assertEqual(run.returncode,2);self.assertIn('cannot be combined',run.stderr)

    def test_native_paused_create_holds_lock_until_pause(self):
        from contextlib import contextmanager
        class Jobs:
            held=False
            calls=[]
            @contextmanager
            def _jobs_lock(self):
                self.held=True
                try:yield
                finally:self.held=False
            def list_jobs(self,include_disabled):
                assert self.held;return []
            def create_job(self,**kwargs):
                assert self.held;self.calls.append('create');return {'id':'one','enabled':True}
            def pause_job(self,jid):
                assert self.held;self.calls.append('pause');return {'id':jid,'enabled':False}
        api=NativeJobs.__new__(NativeJobs);api.jobs=Jobs()
        result=api.call('POST','/api/jobs',{'name':NAME,'enabled':False})
        self.assertEqual(api.jobs.calls,['create','pause']);self.assertFalse(result['job']['enabled'])
        self.assertFalse(api.jobs.held)

    def test_ambiguous_refused(self):
        api=FakeAPI();api.jobs=[{'name':NAME,'id':'a'},{'name':NAME,'id':'b'}]
        with self.assertRaises(ValueError):install(api,Path('/tmp/repo'),Path('/tmp/home'),Path('/tmp/receipt'))

if __name__=='__main__':unittest.main()
