"""Offline compatibility checks: no Hermes, real cron jobs or network."""
import asyncio
import contextvars
from contextlib import contextmanager
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
import threading
import sys
import types
import hashlib
from unittest.mock import patch

path=Path(__file__).resolve().parents[1]/'hermes/plugins/omni-bridge/jobs_api.py'
spec=importlib.util.spec_from_file_location('omni_jobs_api_tested',path)
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

class JobsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.responses=[]; self.fired=[]; self.claimed=False
        self.job={'id':'0123456789ab','enabled':True,'prompt':'offline fixture','origin':{'platform':'api_server'}}
        self.adapter=SimpleNamespace(_background_tasks=set(),_pending_agent_requests=0,
            _check_auth=lambda r:None,_draining_response=lambda:None,
            _check_jobs_available=lambda:None,_check_job_id=lambda r:('0123456789ab',None))
        @contextmanager
        def reserve(adapter):
            adapter._pending_agent_requests+=1
            reservation={'active':True,'detached':False}
            try: yield reservation
            finally:
                if not reservation['detached']: release(adapter,reservation)
        def release(adapter,reservation):
            if reservation['active']:
                reservation['active']=False;adapter._pending_agent_requests-=1
        def claim(ident,return_job):
            if self.claimed or not self.job or not self.job['enabled']:return False
            self.claimed=True
            return dict(self.job,fire_claim={'by':'claim-fixture'})
        self.api=SimpleNamespace(_reserve_pending_api_work=reserve,_release_pending_api_work=release,
            web=SimpleNamespace(json_response=lambda body,status=200:SimpleNamespace(status=status,body=body)))
        self.jobs=SimpleNamespace(get_job=lambda _:self.job,claim_job_for_fire=claim)
        self.handler=module.make_handler(self.api,self.jobs,lambda job:self.fired.append(job))
    async def test_claim_dispatch_and_duplicate(self):
        first=await self.handler(self.adapter,object())
        second=await self.handler(self.adapter,object())
        self.assertEqual(first.status,202);self.assertTrue(first.body['job']['executed'])
        self.assertEqual(first.body['job']['execution_mode'],'background')
        self.assertNotIn('fire_claim',first.body['job']);self.assertEqual(second.status,409)
        self.assertEqual(self.adapter._pending_agent_requests,1)
        await asyncio.gather(*list(self.adapter._background_tasks));await asyncio.sleep(0)
        self.assertEqual(len(self.fired),1);self.assertEqual(self.fired[0]['fire_claim']['by'],'claim-fixture')
        self.assertEqual(self.adapter._pending_agent_requests,0)
    async def test_cancelled_waiter_keeps_reservation_until_worker_finishes(self):
        started=threading.Event(); release=threading.Event()
        def worker(job):
            started.set(); release.wait(3); self.fired.append(job)
        handler=module.make_handler(self.api,self.jobs,worker)
        response=await handler(self.adapter,object())
        self.assertEqual(response.status,202)
        while not started.is_set():await asyncio.sleep(0.001)
        task=next(iter(self.adapter._background_tasks))
        task.cancel();await asyncio.sleep(0.01)
        self.assertEqual(self.adapter._pending_agent_requests,1)
        release.set()
        await asyncio.gather(task,return_exceptions=True);await asyncio.sleep(0)
        self.assertEqual(self.adapter._pending_agent_requests,0)
        self.assertEqual(len(self.fired),1)
    async def test_profile_context_follows_claim_into_worker(self):
        profile=contextvars.ContextVar('offline_profile',default='default')
        token=profile.set('collaboration-profile')
        observed=[]
        handler=module.make_handler(self.api,self.jobs,lambda _:observed.append(profile.get()))
        try:
            self.assertEqual((await handler(self.adapter,object())).status,202)
            await asyncio.gather(*list(self.adapter._background_tasks))
            self.assertEqual(observed,['collaboration-profile'])
        finally:profile.reset(token)
    async def test_auth_and_drain_refuse_before_claim(self):
        denied=SimpleNamespace(status=401)
        self.adapter._check_auth=lambda _:denied
        self.assertIs(await self.handler(self.adapter,object()),denied);self.assertFalse(self.claimed)
        self.adapter._check_auth=lambda _:None;self.adapter._draining_response=lambda:denied
        self.assertIs(await self.handler(self.adapter,object()),denied);self.assertFalse(self.claimed)
    async def test_missing_and_paused_are_not_dispatched(self):
        self.job['enabled']=False
        self.assertEqual((await self.handler(self.adapter,object())).status,409)
        self.job=None
        self.assertEqual((await self.handler(self.adapter,object())).status,404)
        self.assertFalse(self.fired);self.assertEqual(self.adapter._pending_agent_requests,0)
    async def test_claim_failure_is_bounded(self):
        def fail(*a,**k):raise RuntimeError('private detail')
        self.jobs.claim_job_for_fire=fail
        response=await self.handler(self.adapter,object())
        self.assertEqual(response.status,503);self.assertNotIn('private',str(response.body))
        self.assertEqual(self.adapter._pending_agent_requests,0)
    def test_origin_only_local_valid_thread(self):
        origin=module.make_origin(lambda s,r:{'platform':'api_server','chat_id':'api'})
        request=SimpleNamespace(remote='127.0.0.1',headers={'X-Omni-Thread':'omni_0123456789abcdef'})
        self.assertEqual(origin(None,request)['chat_id'],'omni_0123456789abcdef')
        for remote,headers in [('192.0.2.1',request.headers),('127.0.0.1',{'X-Omni-Thread':'omni_0123456789abcdef','X-Forwarded-For':'127.0.0.1'}),('127.0.0.1',{'X-Omni-Thread':'../../bad'}),('127.0.0.1',{})]:
            self.assertEqual(origin(None,SimpleNamespace(remote=remote,headers=headers))['chat_id'],'api')

    async def test_request_guard_prelude_v0_23(self):
        # v0.23.1: one _cron_request_guard call replaces the four split checks.
        calls=[]
        def guard(request,need_job_id=False,check_draining=False):
            calls.append((need_job_id,check_draining));return '0123456789ab',None
        adapter=SimpleNamespace(_background_tasks=set(),_pending_agent_requests=0,_cron_request_guard=guard)
        handler=module.make_handler(self.api,self.jobs,lambda job:self.fired.append(job),'request-guard')
        self.assertEqual((await handler(adapter,object())).status,202)
        self.assertEqual(calls,[(True,True)])
        await asyncio.gather(*list(adapter._background_tasks))
        denied=SimpleNamespace(status=401);self.claimed=False
        adapter._cron_request_guard=lambda r,**k:('0123456789ab',denied)
        self.assertIs(await handler(adapter,object()),denied);self.assertFalse(self.claimed)
    async def test_claims_as_manual_run_when_supported(self):
        # Without manual=True, v0.23 advances next_run_at and the next regular run is skipped.
        seen=[]
        def claim(job_id,*,claim_ttl_seconds=60,force=False,manual=False,return_job=False):
            seen.append(dict(manual=manual,force=force,return_job=return_job));return dict(self.job)
        self.jobs.claim_job_for_fire=claim
        self.assertEqual((await self.handler(self.adapter,object())).status,202)
        self.assertEqual(seen,[{'manual':True,'force':False,'return_job':True}])
        await asyncio.gather(*list(self.adapter._background_tasks))

class InstallTests(unittest.TestCase):
    def test_reviewed_digests_are_the_two_known_upstreams(self):
        self.assertEqual(sorted(module.KNOWN_RUN_HANDLERS.values()),['request-guard','split-checks'])

    def test_disabled_adapter_does_not_import_upstream(self):
        with patch.dict(module.os.environ, {'OMNI_BRIDGE_JOBS_API':'0'}):
            self.assertFalse(module.install())
    def test_unknown_upstream_refuses_and_known_installs_once_before_binding(self):
        class Adapter:
            async def _handle_run_job(self, request):pass
            def _cron_origin_from_request(self, request):return {'platform':'api_server','chat_id':'api'}
        original=Adapter._handle_run_job
        api=types.ModuleType('gateway.platforms.api_server');api.APIServerAdapter=Adapter
        api._reserve_pending_api_work=lambda _:None;api._release_pending_api_work=lambda *a:None
        gateway=types.ModuleType('gateway');platforms=types.ModuleType('gateway.platforms');platforms.api_server=api
        cron=types.ModuleType('cron');cron.jobs=SimpleNamespace()
        tools=types.ModuleType('tools');ct=types.ModuleType('tools.cronjob_tools');ct._run_claimed_job=lambda _:None
        modules={'gateway':gateway,'gateway.platforms':platforms,'gateway.platforms.api_server':api,'cron':cron,'tools':tools,'tools.cronjob_tools':ct}
        with patch.dict(sys.modules,modules),patch.dict(module.os.environ,{'OMNI_BRIDGE_JOBS_API':'1'}),patch.object(module.inspect,'getsource',return_value='fixture'):
            self.assertFalse(module.install());self.assertIs(Adapter._handle_run_job,original)
            with patch.dict(module.KNOWN_RUN_HANDLERS,{hashlib.sha256(b'fixture').hexdigest():'request-guard'}):
                self.assertTrue(module.install())
                bound=Adapter()._handle_run_job
                self.assertIsNot(bound.__func__,original)
                self.assertTrue(module.install());self.assertIs(bound.__func__,Adapter._handle_run_job)

if __name__=='__main__':unittest.main()
