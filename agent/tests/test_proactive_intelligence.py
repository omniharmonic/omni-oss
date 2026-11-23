import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from proactive_intelligence.state import State
from proactive_intelligence.runner import Runner

NOW = 1791720000000

def healthy_bridges(now):
    from datetime import datetime,timezone
    from proactive_intelligence.source_health import EXPECTED_BRIDGES
    return {'checkedAt':datetime.fromtimestamp(now/1000,timezone.utc).isoformat(),'synapse':'healthy','auth':'valid','bridges':{name:'connected' for name in EXPECTED_BRIDGES}}

def event(id='a', revision='r1', **extra):
    return dict(dict(id=id, revision=revision, kind='reply-owed', entity=id, occurred_at=NOW, content='Please confirm the meeting time.', url='parachute://'+id), **extra)


class Reasoner:
    def __init__(self, state=None): self.calls = 0; self.state = state
    def reason(self, packet):
        self.calls += 1
        if self.state:
            self.state.ingest('mail', '2', [event(revision='r2')], now=NOW, coalesce_ms=0)
        return {'decisions': [dict(sourceId=e['id'], sourceRevision=e['revision'], action='work', title='Confirm timing', summary='A meeting needs confirmation.', body='Draft: Thank you; I will confirm shortly.', evidence=[{'sourceId':e['id'],'revision':e['revision']}], expiresAt=NOW+60000) for e in packet['events']]}


class PolicyPublisher:
    def suppressed(self, source_ids):return []


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name)/'state.db'; self.state = State(self.path)
    def tearDown(self): self.state.db.close(); self.temp.cleanup()
    def ingest(self, e): self.state.ingest('mail', '1', [e], now=NOW, coalesce_ms=0)
    def test_unchanged_zero_calls_and_durable_cursor(self):
        self.ingest(event()); reasoner=Reasoner(); runner=Runner(self.state, reasoner, clock=lambda:NOW)
        self.assertEqual(runner.tick()['mode'], 'shadow'); self.ingest(event())
        self.assertFalse(runner.tick()['wakeAgent']); self.assertEqual(reasoner.calls,1)
        reopened=State(self.path); self.assertEqual(json.loads(reopened.db.execute('SELECT cursor FROM sources').fetchone()[0]), '1'); reopened.db.close()
    def test_atomic_budget_and_lease_across_connections(self):
        second=State(self.path)
        for n in range(2):
            self.ingest(event(str(n))); reservation=self.state.reserve(NOW)
            self.assertIsNotNone(reservation); self.assertIsNone(second.reserve(NOW)); self.state.finish(reservation[0],'done')
        self.ingest(event('ordinary')); self.assertIsNone(second.reserve(NOW))
        for n in range(2):
            self.ingest(event('urgent'+str(n), urgent=True, deadline=NOW+30000)); reservation=second.reserve(NOW)
            self.assertIsNotNone(reservation); second.finish(reservation[0],'done')
        self.ingest(event('extra',urgent=True,deadline=NOW+30000)); self.assertIsNone(second.reserve(NOW)); second.db.close()
    def spend(self, at, n=1, **kw):
        for i in range(n):
            self.ingest(event('s%d-%d-%s'%(at,i,kw.get('routine')),**kw)); r=self.state.reserve(at); self.assertIsNotNone(r); self.state.finish(r[0],'done')
    def test_hourly_cap_uses_denver_hour(self):
        self.spend(NOW,2); self.ingest(event('third'))
        self.assertIsNone(self.state.reserve(NOW)); self.assertIsNotNone(self.state.reserve(NOW+3600000))
    def test_daily_nonroutine_cap_is_20(self):
        for h in range(10): self.spend(NOW+h*3600000,2)
        self.ingest(event('late')); self.assertIsNone(self.state.reserve(NOW+10*3600000))
    def test_routine_runs_do_not_consume_nonroutine_budget(self):
        self.spend(NOW,2); self.spend(NOW,1,routine='tweet'); self.spend(NOW,1,routine='nightly-review')
        self.ingest(event('t3',routine='tweet')); self.assertIsNone(self.state.reserve(NOW))
    def test_only_tweet_and_nightly_review_consume_routine_slots(self):
        for i in range(2):
            self.state.db.execute("INSERT INTO runs(id,day,bucket,status,started,until_at,routine) VALUES(?,?,?,?,?,?,?)",('m%d'%i,'2026-10-11','ordinary','done',NOW,NOW,'morning'))
        self.ingest(event('m3',routine='morning')); self.assertIsNone(self.state.reserve(NOW))
        self.ingest(event('t1',routine='tweet')); self.assertIsNotNone(self.state.reserve(NOW))
    def test_stale_evidence_cannot_publish(self):
        self.ingest(event())
        with self.assertRaisesRegex(ValueError,'source changed'):
            Runner(self.state, Reasoner(self.state), clock=lambda:NOW).tick()
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM runs').fetchone()[0],1)
    def test_unknown_outcome_charged_and_retry_ceiling(self):
        self.ingest(event())
        for n in range(3):
            self.assertIsNotNone(self.state.reserve(NOW+n*3600000))
        self.assertIsNone(self.state.reserve(NOW+3*3600000))
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM runs WHERE status='unknown'").fetchone()[0],3)
    def test_time_watcher_restart(self):
        self.ingest(event()); run,_=self.state.reserve(NOW); self.state.finish(run,'done')
        self.state.add_watcher('a','r1','time',NOW+1000)
        reopened=State(self.path); self.assertIsNone(reopened.reserve(NOW)); self.assertIsNotNone(reopened.reserve(NOW+1000)); reopened.db.close()
    def test_dismissal_and_partial_health(self):
        self.ingest(event()); self.state.dismiss('a','r1'); self.ingest(event()); self.assertIsNone(self.state.reserve(NOW))
        self.state.ingest('mail','2',[],health='unavailable',now=NOW)
        self.assertTrue(self.state.fresh([{'sourceId':'a','revision':'r1'}]))
        self.ingest(event(revision='r2')); self.assertIsNotNone(self.state.reserve(NOW))
    def test_ingest_rollback_does_not_advance_cursor(self):
        with self.assertRaises(ValueError): self.state.ingest('mail','bad',[{}],now=NOW)
        self.assertIsNone(self.state.db.execute('SELECT cursor FROM sources').fetchone())

    def test_publication_unknown_retries_exact_without_model(self):
        class Publisher(PolicyPublisher):
            def __init__(self): self.payloads=[]
            def publish(self, decision, event):
                self.payloads.append(json.dumps([decision,event],sort_keys=True))
                if len(self.payloads)==1: raise TimeoutError('unknown')
                return {'ok':True}
        self.ingest(event()); reasoner=Reasoner(); publisher=Publisher()
        runner=Runner(self.state,reasoner,publisher,clock=lambda:NOW)
        runner.tick(live=True)
        reopened=State(self.path)
        Runner(reopened,reasoner,publisher,clock=lambda:NOW).tick(live=True)
        self.assertEqual(reasoner.calls,1); self.assertEqual(publisher.payloads[0],publisher.payloads[1])
        self.assertEqual(reopened.db.execute('SELECT status FROM outbox').fetchone()[0],'published'); reopened.db.close()
    def test_boundary_coalesces_pending_but_done_new_boundary_wakes(self):
        self.ingest(event(boundary='day-before')); self.ingest(event(boundary='hour-before'))
        run,events=self.state.reserve(NOW); self.assertEqual(len(events),1); self.assertEqual(events[0]['boundary'],'hour-before'); self.state.finish(run,'done')
        self.ingest(event(boundary='hour-before')); self.assertIsNone(self.state.reserve(NOW))
        self.ingest(event(boundary='due-now')); self.assertIsNotNone(self.state.reserve(NOW))
    def test_cancellation_invalidates_queue_and_watcher(self):
        self.ingest(event()); self.state.add_watcher('a','r1','time',NOW+1000)
        self.ingest(event(revision='r2',active=False,resolved=True))
        self.assertIsNone(self.state.reserve(NOW+1000)); self.assertFalse(self.state.fresh([{'sourceId':'a','revision':'r2'}]))
    def test_partial_source_holds_pending_and_packet_admission(self):
        self.ingest(event()); self.state.ingest('mail','2',[],health='partial',now=NOW)
        self.assertIsNone(self.state.reserve(NOW))
        self.state.ingest('mail','3',[event()],now=NOW,coalesce_ms=0)
        for n in range(4): self.ingest(event(str(n),content='x'*6000))
        run,events=self.state.reserve(NOW); self.assertLessEqual(len(json.dumps(events)),10000)
    def test_gateway_preserves_kind_and_private_draft(self):
        from proactive_intelligence.runner import GatewayPublisher
        publisher=GatewayPublisher('http://127.0.0.1:8787','fixture')
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'{"ok":true}'
        class Opener:
            def open(self,request,timeout):
                self.payload=json.loads(request.data); return Response()
        opener=Opener(); publisher.opener=opener
        decision=Reasoner().reason({'events':[event()]})['decisions'][0]; decision['draft']='Thank you for the invitation.'
        publisher.publish(decision,event(kind='unprocessed'))
        self.assertEqual(opener.payload['kind'],'unprocessed')
        self.assertIn(decision['draft'],opener.payload['body'])
        self.assertIsInstance(opener.payload['priority'],(int,float))
        self.assertGreaterEqual(opener.payload['priority'],0); self.assertLessEqual(opener.payload['priority'],1)

    def test_shadow_activation_requires_fresh_record_no_second_model(self):
        class Publisher(PolicyPublisher):
            def publish(self,d,e): return {'ok':True}
        self.ingest(event()); reasoner=Reasoner()
        run=Runner(self.state,reasoner,clock=lambda:NOW).tick()['runId']
        receipt=Runner(self.state,None,Publisher(),clock=lambda:NOW).publish_shadow(run)
        self.assertEqual(receipt['publications'],1); self.assertEqual(reasoner.calls,1)
        with self.assertRaises(ValueError): Runner(self.state,None,Publisher(),clock=lambda:NOW).publish_shadow(run)
    def test_targeted_context_does_not_reactivate_completed_source(self):
        from proactive_intelligence.context import ContextAssembler
        from types import SimpleNamespace
        class Client:
            def get_note(self,sid): return SimpleNamespace(id=sid,updated_at='r1',metadata={},tags=[],content='An explicit related fact.')
        completed=event('completed',active=False); self.ingest(completed)
        result=ContextAssembler(Client(),self.state,clock=lambda:NOW).assemble([event(related_ids=['completed','project'])])
        self.assertEqual([e['id'] for e in result['evidence']],['project'])
        self.assertIsNone(self.state.reserve(NOW))
        self.assertFalse(self.state.fresh([{'sourceId':'completed','revision':'r1'}]))
    def test_detector_to_runner_real_batch_contract(self):
        from proactive_intelligence.detectors import select
        from parachute_client import NoteIndex
        note=NoteIndex(id='message',updated_at='2026-10-10T17:00:00Z',metadata={'type':'email','sender':'alex@example.com','subject':'Confirm meeting'},tags=['important'])
        events=select([note],NOW)
        self.state.ingest('vault','cursor',events,now=NOW,coalesce_ms=0)
        receipt=Runner(self.state,Reasoner(),clock=lambda:NOW).tick()
        self.assertEqual(receipt['decisionCount'] if 'decisionCount' in receipt else len(receipt['decisions']),1)

    def test_routine_daily_boundary_and_focus_share_budget(self):
        from proactive_intelligence.routines import build
        from types import SimpleNamespace
        anchor=SimpleNamespace(id='profile',path=None,updated_at='r1',metadata={'type':'profile'},tags=[],content='Synthetic owner profile.')
        related=SimpleNamespace(id='project',path=None,updated_at='2026-10-10T17:00:00Z',metadata={'type':'report'},tags=[],content='Synthetic project report.')
        class Client:
            def query_notes(self,**kwargs): return [anchor,related]
            def get_note(self,sid): return anchor
        routine=build(Client(),'nightly-review',NOW)
        self.assertTrue(routine['boundary'].startswith('routine:nightly-review:'))
        self.assertEqual(routine['related_ids'],['project'])
        self.ingest(event('other'))
        self.state.ingest('routine:nightly-review',routine['boundary'],[routine],now=NOW,coalesce_ms=0)
        reasoner=Reasoner(); receipt=Runner(self.state,reasoner,clock=lambda:NOW).tick(focus_boundary=routine['boundary'],routine='nightly-review')
        self.assertEqual(receipt['decisions'][0]['sourceId'],'profile')
        self.assertEqual(self.state.db.execute('SELECT bucket FROM runs').fetchone()[0],'ordinary')
        again=Runner(self.state,reasoner,clock=lambda:NOW).tick(focus_boundary=routine['boundary'],routine='nightly-review')
        self.assertFalse(again['wakeAgent']); self.assertEqual(reasoner.calls,1)
    def test_tweet_sources_only_from_writing_and_recent_insight(self):
        from proactive_intelligence.routines import tweet_sources
        from types import SimpleNamespace
        from datetime import datetime,timezone
        iso=lambda ms: datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
        row=lambda i,tags,age=1: SimpleNamespace(id=i,path=i,updated_at=iso(NOW-age*86400000),metadata={},tags=tags)
        rows=[row('w',['writing']),row('c',['concept']),row('e',['email']),row('t',['transcript']),
              row('i-new',['agent-insight'],2),row('i-old',['agent-insight'],30),row('both',['writing','person'])]
        self.assertEqual(sorted(tweet_sources(rows,NOW)),['c','i-new','w'])
    def test_tweet_sources_rotate_by_day(self):
        from proactive_intelligence.routines import tweet_sources
        from types import SimpleNamespace
        rows=[SimpleNamespace(id='n%d'%i,path='n%d'%i,updated_at='2026-10-10T00:00:00Z',metadata={},tags=['concept']) for i in range(5)]
        self.assertNotEqual(tweet_sources(rows,NOW)[0],tweet_sources(rows,NOW+86400000)[0])
    def test_scrub_holds_draft_with_person_name(self):
        import types
        from unittest.mock import patch
        from proactive_intelligence.routines import tweet_privacy_hits
        from proactive_intelligence.runner import GatewayPublisher
        self.assertEqual(len(tweet_privacy_hits('Great call with Sam Rivera today',['Sam Rivera'])),1)
        self.assertEqual(tweet_privacy_hits('Coordination is a commons',['Sam Rivera']),[])
        self.assertTrue(tweet_privacy_hits('write me at a@b.co',[])); self.assertTrue(tweet_privacy_hits('call 415-555-0100',[]))
        calls=[]
        fake=types.SimpleNamespace(publish_routine=lambda *a,**k: calls.append(a) or {},routine_title=lambda t,d:t)
        decision={'body':'Inspired by the commons essay.','draft':'Great call with Sam Rivera today'}
        with patch.dict(sys.modules,{'omni_routine':fake}):
            GatewayPublisher('http://127.0.0.1','t',names=lambda:['Sam Rivera']).publish_routine_tweet(decision,{'routine':'tweet','boundary':'routine:tweet:2026-10-10'})
        args=calls[0]
        self.assertIsNone(args[6]); self.assertIn('held back',args[4]); self.assertIn('commons essay',args[4])
    def test_tweet_person_names_never_reach_packet_or_run_result(self):
        import types
        from unittest.mock import patch
        from types import SimpleNamespace
        from datetime import datetime,timezone
        from proactive_intelligence.routines import build, person_names
        from proactive_intelligence.context import ContextAssembler
        from proactive_intelligence.runner import GatewayPublisher, SYSTEM
        iso=lambda ms:datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
        anchor=SimpleNamespace(id='profile',path='profile',updated_at=iso(NOW),metadata={'type':'profile'},tags=[],content='Synthetic profile')
        people=[SimpleNamespace(id='p%04d'%i,path='vault/people/p%04d'%i,updated_at=iso(NOW),metadata={'name':'Zorvex%04d Quillamere'%i},tags=['person']) for i in range(1000)]
        writing=[SimpleNamespace(id='w%d'%i,path='w%d'%i,updated_at=iso(NOW),metadata={'type':'writing'},tags=['writing'],content=('Essay %d on commons and coordination. '%i)*40) for i in range(5)]
        by_id={n.id:n for n in [anchor]+people+writing}
        class Client:
            def query_notes(self,tag=None,**kwargs):return [n for n in by_id.values() if tag is None or tag in (n.tags or [])]
            def get_note(self,sid):return by_id.get(sid)
        routine=build(Client(),'tweet',NOW)
        self.assertNotIn('private_names',routine)
        self.assertEqual(sorted(routine['related_ids']),['w%d'%i for i in range(5)])
        # A row queued before the names moved off the event is stripped as well.
        routine['private_names']=[p.metadata['name'] for p in people]
        self.state.ingest('routine:tweet',routine['boundary'],[routine],now=NOW,coalesce_ms=0)
        packets=[]
        class Capture(Reasoner):
            def reason(self,packet):packets.append(packet);return super().reason(packet)
        Runner(self.state,Capture(),clock=lambda:NOW,context_assembler=ContextAssembler(Client(),self.state,clock=lambda:NOW)).tick(routine='tweet')
        blob=json.dumps(packets[0])
        self.assertLessEqual(len(blob),min(20000,24000-len(SYSTEM)))
        self.assertEqual(sorted(e['id'] for e in packets[0]['relatedEvidence']),['w%d'%i for i in range(5)])
        stored=self.state.db.execute('SELECT result FROM runs').fetchone()[0]
        self.assertNotIn('Quillamere',blob); self.assertNotIn('Quillamere',stored)
        names=person_names(Client()); self.assertEqual(len(names),1000)
        calls=[]
        fake=types.SimpleNamespace(publish_routine=lambda *a,**k: calls.append(a) or {},routine_title=lambda t,d:t)
        with patch.dict(sys.modules,{'omni_routine':fake}):
            GatewayPublisher('http://127.0.0.1','t',names=lambda:person_names(Client())).publish_routine_tweet(
                {'body':'From the commons essay.','draft':'Great call with Zorvex0421 Quillamere today'},{'routine':'tweet','boundary':routine['boundary']})
        self.assertIsNone(calls[0][6]); self.assertIn('held back',calls[0][4])
    def test_tweet_scrub_fails_closed_without_names(self):
        import types
        from unittest.mock import patch
        from proactive_intelligence.runner import GatewayPublisher
        def broken(): raise ValueError('person inventory incomplete')
        for names in (None,broken):
            calls=[]
            fake=types.SimpleNamespace(publish_routine=lambda *a,**k: calls.append(a) or {},routine_title=lambda t,d:t)
            with patch.dict(sys.modules,{'omni_routine':fake}):
                GatewayPublisher('http://127.0.0.1','t',names=names).publish_routine_tweet({'body':'Why.','draft':'Commons are verbs.'},{'routine':'tweet','boundary':'routine:tweet:2026-10-10'})
            self.assertIsNone(calls[0][6]); self.assertIn('held back',calls[0][4])
        calls=[]
        fake=types.SimpleNamespace(publish_routine=lambda *a,**k: calls.append(a) or {},routine_title=lambda t,d:t)
        with patch.dict(sys.modules,{'omni_routine':fake}):
            GatewayPublisher('http://127.0.0.1','t',names=lambda:['Sam Rivera']).publish_routine_tweet({'body':'Why.','draft':'Commons are verbs.'},{'routine':'tweet','boundary':'routine:tweet:2026-10-10'})
        self.assertEqual(calls[0][6]['payload']['text'],'Commons are verbs.')
    def test_scrub_ignores_single_word_names_and_dates(self):
        from proactive_intelligence.routines import tweet_privacy_hits as h
        self.assertEqual(h('Will this scale?',['Will']),[])
        self.assertEqual(len(h('Thanks Sam Rivera',['Sam Rivera','Will'])),1)
        self.assertEqual(h('Shipped on 2026-10-22',[]),[]); self.assertEqual(h('2025-2030 horizon',[]),[])
        self.assertEqual(len(h('call 415-555-0142',[])),1)
    def test_tweet_prompt_drafts_by_default(self):
        from proactive_intelligence.routines import TASKS
        self.assertNotIn('public source',TASKS['tweet'])
        self.assertIn('ignore',TASKS['tweet']); self.assertIn('only when the supplied notes contain no usable idea',TASKS['tweet'])
    def test_routine_keeps_old_revision_meeting_deadline_and_excludes_ancient_report(self):
        from proactive_intelligence.routines import build
        from parachute_client import NoteIndex
        from types import SimpleNamespace
        from datetime import datetime,timezone
        iso=lambda milliseconds: datetime.fromtimestamp(milliseconds/1000,timezone.utc).isoformat()
        anchor=SimpleNamespace(id='profile',path=None,updated_at=iso(NOW),metadata={'type':'profile'},tags=[],content='Synthetic profile')
        meeting=NoteIndex(id='meeting',updated_at='2026-09-01T00:00:00Z',metadata={'type':'meeting','calendarEventId':'calendar','start':iso(NOW+2*86400000),'end':iso(NOW+2*86400000+3600000)})
        task=NoteIndex(id='task',updated_at='2026-09-01T00:00:00Z',metadata={'type':'task','deadline':iso(NOW+86400000),'deadline_source':'stated','status':'pending'})
        old=NoteIndex(id='old-report',updated_at='2026-01-01T00:00:00Z',metadata={'type':'report'})
        class Client:
            def query_notes(self,**kwargs): return [anchor,meeting,task,old]
            def get_note(self,sid): return anchor
        sources=build(Client(),'morning',NOW)['related_ids']
        self.assertIn('meeting',sources); self.assertIn('task',sources); self.assertNotIn('old-report',sources)

    def test_deadline_baseline_queue_commit_is_atomic(self):
        self.state.ingest('mail','1',[event()],now=NOW,coalesce_ms=0,deadline_updates={'a':'first'})
        with self.assertRaises(ValueError): self.state.ingest('mail','2',[{}],now=NOW,deadline_updates={'a':'lost'})
        self.assertEqual(self.state.db.execute('SELECT value FROM detector_deadlines WHERE id=?',('a',)).fetchone()[0],'first')
        self.state.ingest('mail','2',[],health='partial',now=NOW,deadline_updates={'a':'lost'})
        self.assertEqual(self.state.db.execute('SELECT value FROM detector_deadlines WHERE id=?',('a',)).fetchone()[0],'first')

    def test_hourly_snapshot_reasons_on_first_tick_without_extra_hour(self):
        from proactive_intelligence.__main__ import ingest_reconciled
        batch={'source':'mail','cursor':'first','events':[event()], 'health':'healthy'}
        ingest_reconciled(self.state,batch,NOW)
        reasoner=Reasoner(); runner=Runner(self.state,reasoner,clock=lambda:NOW)
        self.assertTrue(runner.tick()['wakeAgent']); self.assertEqual(reasoner.calls,1)
        ingest_reconciled(self.state,batch,NOW+3600000)
        self.assertFalse(Runner(self.state,reasoner,clock=lambda:NOW+3600000).tick()['wakeAgent'])
        self.assertEqual(reasoner.calls,1)
    def test_event_ingestion_keeps_burst_coalescing_default(self):
        self.state.ingest('mail','first',[event()],now=NOW)
        self.assertIsNone(self.state.reserve(NOW))
        self.assertIsNotNone(self.state.reserve(NOW+180000))

    def test_unknown_sent_evidence_spends_zero_model_budget(self):
        from proactive_intelligence.replies import ReplyGuard
        self.ingest(event(channel='email',mail_account='owner@example.com',mail_thread_id='thread',inbound_at=NOW))
        reasoner=Reasoner(); guard=ReplyGuard(fetch=lambda accounts:{'owner@example.com':None})
        receipt=Runner(self.state,reasoner,clock=lambda:NOW,reply_guard=guard).tick()
        self.assertFalse(receipt['wakeAgent']);self.assertEqual(reasoner.calls,0)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM runs').fetchone()[0],0)
        self.assertEqual(self.state.db.execute("SELECT health FROM sources WHERE name='canonical-replies'").fetchone()[0],'unavailable')
    def test_sent_reply_during_preparation_blocks_same_revision_draft(self):
        from proactive_intelligence.replies import ReplyGuard
        sent={};calls=[]
        def fetch(accounts):calls.append(accounts);return {'owner@example.com':dict(sent)}
        class PreparedReasoner(Reasoner):
            def reason(self,packet):
                result=super().reason(packet);sent['thread']=NOW+1;return result
        class Publisher(PolicyPublisher):
            def __init__(self):self.published=[];self.resolved=[]
            def publish(self,d,e):self.published.append(d);return {'ok':True}
            def resolve_sources(self,refs):self.resolved.extend(refs);return {'resolved':[r['sourceId'] for r in refs]}
        self.ingest(event(channel='email',mail_account='owner@example.com',mail_thread_id='thread',inbound_at=NOW))
        publisher=Publisher();reasoner=PreparedReasoner()
        Runner(self.state,reasoner,publisher,clock=lambda:NOW,reply_guard=ReplyGuard(fetch=fetch)).tick(live=True)
        self.assertEqual(reasoner.calls,1);self.assertEqual(publisher.published,[])
        self.assertEqual(publisher.resolved,[{'sourceId':'a','sourceRevision':'r1'}])
        self.assertEqual(self.state.db.execute('SELECT status FROM outbox').fetchone()[0],'stale')
        self.assertEqual(len(calls),2)
    def test_prior_sent_message_does_not_resolve_new_inbound(self):
        from proactive_intelligence.replies import ReplyGuard
        mail=event(channel='email',mail_account='owner@example.com',mail_thread_id='thread',inbound_at=NOW)
        checked=ReplyGuard(fetch=lambda accounts:{'owner@example.com':{'thread':NOW-1}}).check([mail])
        self.assertEqual(checked['eligible'],[mail]);self.assertEqual(checked['replied'],[])
    def test_deferred_routine_keeps_goal_and_never_mixes_source_cards(self):
        from proactive_intelligence.routines import TASKS
        packets=[]
        class Capture(Reasoner):
            def reason(self,packet):packets.append(packet);return super().reason(packet)
        self.ingest(event('ordinary',occurred_at=NOW-1))
        self.ingest(event('profile',routine='nightly-review',boundary='routine:nightly-review:day'))
        receipt=Runner(self.state,Capture(),clock=lambda:NOW).tick()
        self.assertEqual(len(receipt['decisions']),1)
        self.assertEqual(packets[0]['task'],TASKS['nightly-review'])
        self.assertEqual(packets[0]['events'][0]['id'],'profile')
    def test_two_change_runs_in_an_hour_leave_routine_slot(self):
        for n in range(2):
            self.ingest(event(str(n)));run,_=self.state.reserve(NOW);self.state.finish(run,'done')
        self.ingest(event('third-change'));self.assertIsNone(self.state.reserve(NOW))
        self.ingest(event('profile',routine='tweet',boundary='routine:tweet:day'))
        run,events=self.state.reserve(NOW);self.assertEqual(events[0]['routine'],'tweet');self.state.finish(run,'done')
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM runs').fetchone()[0],3)

    def test_hourly_reconciliation_resolves_published_email_on_separate_sent_reply(self):
        from proactive_intelligence.__main__ import ingest_reconciled
        from proactive_intelligence.replies import ReplyGuard
        mail=event(channel='email',mail_account='owner@example.com',mail_thread_id='thread',inbound_at=NOW)
        self.ingest(mail);reasoner=Reasoner()
        Runner(self.state,reasoner,clock=lambda:NOW).tick()
        events=ingest_reconciled(self.state,{'source':'mail','cursor':'same','events':[mail],'health':'healthy'},NOW+3600000,ReplyGuard(fetch=lambda accounts:{'owner@example.com':{'thread':NOW+1}}))
        self.assertFalse(events[0]['active']);self.assertTrue(events[0]['resolved'])
        self.assertFalse(self.state.fresh([{'sourceId':'a','revision':'r1'}]))
        self.assertEqual(reasoner.calls,1)

    def test_shadow_activation_rechecks_canonical_sent_before_publication(self):
        from proactive_intelligence.replies import ReplyGuard
        class Publisher(PolicyPublisher):
            def __init__(self):self.published=0
            def publish(self,d,e):self.published+=1;return {'ok':True}
            def resolve_sources(self,refs):return {'resolved':refs}
        self.ingest(event(channel='email',mail_account='owner@example.com',mail_thread_id='thread',inbound_at=NOW))
        run=Runner(self.state,Reasoner(),clock=lambda:NOW).tick()['runId']
        publisher=Publisher();guard=ReplyGuard(fetch=lambda accounts:{'owner@example.com':{'thread':NOW+1}})
        with self.assertRaisesRegex(ValueError,'cited source'):
            Runner(self.state,None,publisher,clock=lambda:NOW,reply_guard=guard).publish_shadow(run)
        self.assertEqual(publisher.published,0)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_time_wait_survives_eligibility_cutoff_and_refreshes_revision(self):
        self.ingest(event());run,_=self.state.reserve(NOW);self.state.finish(run,'done')
        self.state.add_watcher('a','r1','time',NOW+1000)
        self.ingest(event(active=False))
        self.assertEqual(self.state.db.execute('SELECT status FROM watchers').fetchone()[0],'waiting')
        self.ingest(event(revision='r2',active=False))
        self.assertEqual(self.state.db.execute('SELECT revision FROM watchers').fetchone()[0],'r2')
        run,events=self.state.reserve(NOW+1000)
        self.assertEqual(events[0]['revision'],'r2');self.assertTrue(events[0]['active'])
        self.assertTrue(self.state.fresh([{'sourceId':'a','revision':'r2'}]))
    def test_revision_wait_wakes_on_real_revision_even_outside_default_eligibility(self):
        self.ingest(event());run,_=self.state.reserve(NOW);self.state.finish(run,'done')
        self.state.add_watcher('a','r1','revision')
        self.ingest(event(revision='r2',active=False))
        run,events=self.state.reserve(NOW)
        self.assertEqual(events[0]['revision'],'r2');self.assertTrue(events[0]['boundary'].startswith('watch:'))
    def test_failed_validation_keeps_actual_usage_and_elapsed(self):
        class Ungrounded(Reasoner):
            def reason(self,packet):
                result=super().reason(packet);result['usage']={'inputTokens':123,'outputTokens':45}
                result['decisions'][0]['evidence']=[{'sourceId':'invented','revision':'fake'}]
                return result
        self.ingest(event())
        with self.assertRaises(ValueError):Runner(self.state,Ungrounded(),clock=lambda:NOW).tick()
        row=self.state.db.execute('SELECT usage,result FROM runs').fetchone()
        self.assertEqual(json.loads(row['usage'])['inputTokens'],123)
        self.assertIn('elapsedMs',json.loads(row['result']))
        self.assertEqual(json.loads(row['result'])['validation']['expectedDecisions'],1)
        self.assertEqual(json.loads(row['result'])['validation']['returnedDecisions'],1)
        self.assertNotIn('invented',row['result'])
    def test_gateway_dismissal_prevents_model_budget_spend(self):
        class Publisher(PolicyPublisher):
            def suppressed(self,ids):return ids
        self.ingest(event());reasoner=Reasoner()
        receipt=Runner(self.state,reasoner,Publisher(),clock=lambda:NOW).tick(live=True)
        self.assertFalse(receipt['wakeAgent']);self.assertEqual(reasoner.calls,0)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM runs').fetchone()[0],0)
        self.assertEqual(self.state.db.execute('SELECT status FROM queue').fetchone()[0],'dismissed')
    def test_unavailable_suppression_check_fails_before_budget(self):
        class Publisher(PolicyPublisher):
            def suppressed(self,ids):raise TimeoutError()
        self.ingest(event());reasoner=Reasoner()
        with self.assertRaisesRegex(RuntimeError,'suppression_unavailable'):Runner(self.state,reasoner,Publisher(),clock=lambda:NOW).tick(live=True)
        self.assertEqual(reasoner.calls,0);self.assertEqual(self.state.db.execute('SELECT count(*) FROM runs').fetchone()[0],0)

    def test_matrix_failure_blocks_matrix_primary_but_tasks_still_reason(self):
        from proactive_intelligence.source_health import SourceHealthGuard
        from datetime import datetime,timezone
        iso=lambda ms:datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
        guard=SourceHealthGuard(fetch=lambda:{'checkedAt':iso(NOW),'sources':[{'name':'matrix','status':'failing','kind':'server','lastSuccessAt':iso(NOW-250000),'staleAfterMs':60000,'failureStreak':3,'reconcileOwed':0,'reconcileStuck':0}]},clock=lambda:NOW)
        packets=[]
        class Capture(Reasoner):
            def reason(self,packet):packets.append(packet);return super().reason(packet)
        self.ingest(event('matrix',channel='message-thread'))
        self.ingest(event('task',kind='task',channel='task',deadline=NOW+60000))
        receipt=Runner(self.state,Capture(),clock=lambda:NOW,source_health_guard=guard).tick()
        self.assertEqual(receipt['decisions'][0]['sourceId'],'task')
        self.assertFalse(packets[0]['sourceHealth']['matrix']['healthy'])
        self.assertIn('do not infer',packets[0]['sourceHealthWarning'].lower())
        self.assertEqual(self.state.db.execute("SELECT health FROM sources WHERE name='ingestion:matrix'").fetchone()[0],'unavailable')
    def test_matrix_health_requires_real_recent_success_and_complete_reconcile(self):
        from proactive_intelligence.source_health import SourceHealthGuard
        from datetime import datetime,timezone
        iso=lambda ms:datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
        base={'name':'matrix','status':'ok','kind':'server','lastSuccessAt':iso(NOW-1000),'staleAfterMs':60000,'failureStreak':0,'reconcileOwed':0,'reconcileStuck':0}
        for override in ({'lastSuccessAt':None},{'lastSuccessAt':iso(NOW-120000)},{'failureStreak':1},{'reconcileOwed':1},{'reconcileStuck':1},{'status':'disabled'}):
            source=dict(base,**override)
            guard=SourceHealthGuard(fetch=lambda s=source:{'checkedAt':iso(NOW),'sources':[s]},clock=lambda:NOW)
            self.assertFalse(guard.refresh()['healthy'])
        guard=SourceHealthGuard(fetch=lambda:{'checkedAt':iso(NOW),'sources':[base]},clock=lambda:NOW,fetch_bridge=lambda:healthy_bridges(NOW))
        self.assertTrue(guard.refresh()['healthy'])
        self.assertEqual(guard.filter([event(channel='message-thread')])['blocked'],[])
    def test_matrix_unknown_related_evidence_is_removed_and_packet_warns(self):
        from proactive_intelligence.source_health import SourceHealthGuard
        packets=[]
        class Context:
            def assemble(self,events):return {'evidence':[event('relatedMatrix',channel='message-thread')],'health':'healthy','toolReads':1}
        class Capture(Reasoner):
            def reason(self,packet):packets.append(packet);return super().reason(packet)
        self.ingest(event('task',kind='task'))
        Runner(self.state,Capture(),clock=lambda:NOW,context_assembler=Context(),source_health_guard=SourceHealthGuard(clock=lambda:NOW)).tick()
        self.assertEqual(packets[0]['relatedEvidence'],[])
        self.assertEqual(packets[0]['relatedContextHealth'],'partial')
    def test_same_revision_queued_payload_enrichment_cannot_bypass_matrix_gate(self):
        from proactive_intelligence.source_health import SourceHealthGuard
        self.ingest(event('matrix'))
        self.state.ingest('mail','same',[event('matrix',channel='message-thread',queued=False)],now=NOW,coalesce_ms=0)
        reasoner=Reasoner()
        receipt=Runner(self.state,reasoner,clock=lambda:NOW,source_health_guard=SourceHealthGuard(clock=lambda:NOW)).tick()
        self.assertFalse(receipt['wakeAgent']);self.assertEqual(reasoner.calls,0)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM runs').fetchone()[0],0)

    def test_related_matrix_becomes_unhealthy_during_model_blocks_task_publication(self):
        from proactive_intelligence.source_health import SourceHealthGuard
        from datetime import datetime,timezone
        iso=lambda ms:datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
        status=['ok']
        source=event('matrix-related',channel='message-thread',queued=False)
        self.state.ingest('context','1',[source],now=NOW,coalesce_ms=0)
        self.ingest(event('task',kind='task'))
        class Context:
            def assemble(self,events):return {'evidence':[source],'health':'healthy','toolReads':1}
        class Cite(Reasoner):
            def reason(self,packet):
                result=super().reason(packet)
                result['decisions'][0]['evidence'].append({'sourceId':'matrix-related','revision':'r1'})
                status[0]='failing';return result
        class Publisher(PolicyPublisher):
            def __init__(self):self.published=[]
            def publish(self,d,e):self.published.append(d);return {'ok':True}
        guard=SourceHealthGuard(fetch=lambda:{'checkedAt':iso(NOW),'sources':[{'name':'matrix','kind':'server','status':status[0],'lastSuccessAt':iso(NOW-1000),'staleAfterMs':60000}]},clock=lambda:NOW,fetch_bridge=lambda:healthy_bridges(NOW))
        publisher=Publisher();reasoner=Cite()
        Runner(self.state,reasoner,publisher,clock=lambda:NOW,context_assembler=Context(),source_health_guard=guard).tick(live=True)
        self.assertEqual(reasoner.calls,1);self.assertEqual(publisher.published,[])
        self.assertEqual(self.state.db.execute('SELECT status FROM outbox').fetchone()[0],'pending')
        self.assertEqual(self.state.db.execute('SELECT attempts FROM outbox').fetchone()[0],0)
    def test_shadow_task_citing_unknown_matrix_cannot_activate(self):
        from proactive_intelligence.source_health import SourceHealthGuard
        source=event('matrix-related',channel='message-thread',queued=False)
        self.state.ingest('context','1',[source],now=NOW,coalesce_ms=0);self.ingest(event('task',kind='task'))
        class Context:
            def assemble(self,events):return {'evidence':[source],'health':'healthy','toolReads':1}
        class Cite(Reasoner):
            def reason(self,packet):
                result=super().reason(packet);result['decisions'][0]['evidence'].append({'sourceId':'matrix-related','revision':'r1'});return result
        class Publisher(PolicyPublisher):
            def publish(self,d,e):raise AssertionError('must not publish')
        run=Runner(self.state,Cite(),clock=lambda:NOW,context_assembler=Context()).tick()['runId']
        with self.assertRaisesRegex(ValueError,'cited source'):
            Runner(self.state,None,Publisher(),clock=lambda:NOW,source_health_guard=SourceHealthGuard(clock=lambda:NOW)).publish_shadow(run)
        self.assertEqual(json.loads(self.state.db.execute('SELECT result FROM runs').fetchone()[0])['mode'],'shadow')
    def test_related_mail_reply_during_model_invalidates_task_decision(self):
        from proactive_intelligence.replies import ReplyGuard
        sent={}
        source=event('mail-related',channel='email',mail_account='owner@example.com',mail_thread_id='thread',inbound_at=NOW,queued=False)
        self.state.ingest('context','1',[source],now=NOW,coalesce_ms=0);self.ingest(event('task',kind='task'))
        class Context:
            def assemble(self,events):return {'evidence':[source],'health':'healthy','toolReads':1}
        class Cite(Reasoner):
            def reason(self,packet):
                result=super().reason(packet);result['decisions'][0]['evidence'].append({'sourceId':'mail-related','revision':'r1'});sent['thread']=NOW+1;return result
        class Publisher(PolicyPublisher):
            def publish(self,d,e):raise AssertionError('must not publish')
        guard=ReplyGuard(fetch=lambda accounts:{'owner@example.com':dict(sent)})
        Runner(self.state,Cite(),Publisher(),clock=lambda:NOW,context_assembler=Context(),reply_guard=guard).tick(live=True)
        self.assertEqual(self.state.db.execute('SELECT status FROM outbox').fetchone()[0],'stale')

    def test_incomplete_decision_coverage_preserves_counts_not_private_ids(self):
        class Partial(Reasoner):
            def reason(self,packet):
                result=super().reason(packet);result['decisions']=result['decisions'][:1];result['usage']={'inputTokens':5583,'outputTokens':2740};return result
        self.ingest(event('private-source-one'));self.ingest(event('private-source-two'))
        with self.assertRaisesRegex(ValueError,'expected=2, returned=1, missing=1'):
            Runner(self.state,Partial(),clock=lambda:NOW).tick()
        row=self.state.db.execute('SELECT result,usage FROM runs').fetchone();diagnostics=json.loads(row['result'])
        self.assertEqual(diagnostics['validation']['expectedDecisions'],2)
        self.assertEqual(diagnostics['validation']['returnedDecisions'],1)
        self.assertEqual(diagnostics['validation']['missingDecisions'],1)
        self.assertNotIn('private-source',row['result']);self.assertEqual(json.loads(row['usage'])['outputTokens'],2740)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM queue WHERE status='queued'").fetchone()[0],2)
    def test_output_contract_accounts_every_primary_and_combined_input_fits(self):
        from proactive_intelligence.runner import SYSTEM
        packets=[]
        class Capture(Reasoner):
            def reason(self,packet):packets.append(packet);return super().reason(packet)
        self.ingest(event('one'));self.ingest(event('two'))
        Runner(self.state,Capture(),clock=lambda:NOW).tick('背景'*10000)
        packet=packets[0]
        self.assertEqual(packet['outputContract']['expectedDecisions'],2)
        self.assertEqual({e['sourceId'] for e in packet['outputContract']['primarySources']},{'one','two'})
        self.assertLessEqual(len(SYSTEM)+len(json.dumps(packet)),24000)
        self.assertIn('EXACTLY ONE decision for EVERY primary',SYSTEM)
        self.assertIn('NO raw source IDs',SYSTEM)
    def test_local_calendar_day_is_denver_even_after_utc_midnight(self):
        from proactive_intelligence.detectors import stamp
        local_time=stamp('2026-10-12T02:00:00Z');packets=[]
        class Capture(Reasoner):
            def reason(self,packet):
                packets.append(packet);result=super().reason(packet)
                for decision in result['decisions']:decision['expiresAt']=packet['now']+60000
                return result
        self.state.ingest('mail','1',[event()],now=local_time,coalesce_ms=0)
        Runner(self.state,Capture(),clock=lambda:local_time).tick()
        self.assertEqual(packets[0]['timezone'],'America/Edmonton')
        self.assertTrue(packets[0]['localNow'].startswith('2026-10-11T20:00:00'))
        self.assertTrue(packets[0]['localNow'].endswith('-06:00'))
    def test_routine_anchor_body_and_expired_concern_cannot_be_priority_evidence(self):
        from proactive_intelligence.routines import build
        from types import SimpleNamespace
        from datetime import datetime,timezone
        iso=lambda ms:datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
        anchor=SimpleNamespace(id='a-ancient-anchor',path=None,updated_at='2026-06-01T00:00:00Z',metadata={'type':'concern','deadline':'2026-06-05'},tags=[],content='OLD JUNE DEADLINE SHOULD NEVER DOMINATE')
        old=SimpleNamespace(id='another-expired-concern',path=None,updated_at=iso(NOW),metadata={'type':'concern','deadline':'2026-06-05'},tags=[])
        report=SimpleNamespace(id='current-report',path=None,updated_at=iso(NOW),metadata={'type':'report'},tags=[])
        class Client:
            def query_notes(self,**kwargs):return [anchor,old,report]
            def get_note(self,sid):return anchor
        routine=build(Client(),'nightly-review',NOW)
        self.assertNotIn(anchor.content,routine['content'])
        self.assertIn('Routing anchor only',routine['content'])
        self.assertEqual(routine['related_ids'],['current-report'])
        self.state.ingest('routine',routine['boundary'],[routine],now=NOW,coalesce_ms=0)
        packets=[]
        class Capture(Reasoner):
            def reason(self,packet):packets.append(packet);return super().reason(packet)
        Runner(self.state,Capture(),clock=lambda:NOW).tick(routine='nightly-review')
        self.assertTrue(packets[0]['routingAnchorOnly'])

    def test_related_calendar_keeps_record_time_and_conflicting_body(self):
        from proactive_intelligence.context import ContextAssembler
        from types import SimpleNamespace
        note=SimpleNamespace(id='meeting',updated_at='r1',metadata={'type':'meeting','title':'Product Tactical','start':'2026-10-12T18:00:00Z','end':'2026-10-12T18:30:00Z','status':'confirmed','calendarEventId':'calendar-event','meetLink':'https://example.test/meeting'},tags=[],content='The old note says Product Tactical starts at 12:15.')
        class Client:
            def get_note(self,sid):return note
        evidence=ContextAssembler(Client(),self.state,clock=lambda:NOW).assemble([event(related_ids=['meeting'])])['evidence'][0]
        self.assertEqual(evidence['calendarFields']['start'],'2026-10-12T18:00:00Z')
        self.assertEqual(evidence['calendarFields']['end'],'2026-10-12T18:30:00Z')
        self.assertEqual(evidence['calendarFields']['status'],'confirmed')
        self.assertEqual(evidence['calendarFields']['calendarEventId'],'calendar-event')
        self.assertIn('12:15',evidence['content'])
        self.assertEqual(evidence['calendarFields']['meetLink'],'https://example.test/meeting')
    def test_calendar_metadata_bounded_without_losing_start_end(self):
        from proactive_intelligence.context import calendar_fields
        fields=calendar_fields({'type':'meeting','start':'2026-10-12T18:00:00Z','end':'2026-10-12T18:30:00Z','calendarEventId':'event','meetLink':'x'*5000,'location':'x'*5000})
        self.assertLessEqual(len(json.dumps(fields)),1500)
        self.assertEqual(fields['start'],'2026-10-12T18:00:00Z');self.assertEqual(fields['end'],'2026-10-12T18:30:00Z')
    def test_routine_calendar_subset_reports_available_requested_and_included(self):
        from proactive_intelligence.routines import build
        from proactive_intelligence.runner import SYSTEM
        from parachute_client import NoteIndex
        from types import SimpleNamespace
        from datetime import datetime,timezone
        iso=lambda ms:datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
        anchor=SimpleNamespace(id='profile',path=None,updated_at=iso(NOW),metadata={'type':'profile'},tags=[],content='Synthetic profile')
        meetings=[NoteIndex(id='meeting-'+str(i),updated_at=iso(NOW),metadata={'type':'meeting','calendarEventId':'event-'+str(i),'start':iso(NOW+86400000+i*3600000),'end':iso(NOW+86400000+i*3600000+1200000)}) for i in range(5)]
        tasks=[NoteIndex(id='task-'+str(i),updated_at=iso(NOW),metadata={'type':'task','status':'pending','deadline':iso(NOW+86400000),'deadline_source':'stated'}) for i in range(3)]
        mails=[NoteIndex(id='mail-'+str(i),updated_at=iso(NOW),metadata={'type':'email','sender':'alex@example.test'},tags=['important']) for i in range(3)]
        reports=[NoteIndex(id='report-'+str(i),updated_at=iso(NOW),metadata={'type':'report'}) for i in range(4)]
        class Client:
            def query_notes(self,**kwargs):return [anchor]+meetings+tasks+mails+reports
            def get_note(self,sid):return anchor
        routine=build(Client(),'nightly-review',NOW)
        self.assertEqual(routine['calendar_coverage']['availableMeetingRecords'],5)
        self.assertEqual(routine['calendar_coverage']['requestedMeetingRecords'],2)
        source=event('meeting-0',kind='context',channel='meeting',calendarFields={'start':meetings[0].metadata['start']})
        class Context:
            def assemble(self,events):return {'evidence':[source],'health':'partial','toolReads':1}
        packets=[]
        class Capture(Reasoner):
            def reason(self,packet):packets.append(packet);return super().reason(packet)
        self.state.ingest('routine',routine['boundary'],[routine],now=NOW,coalesce_ms=0)
        Runner(self.state,Capture(),clock=lambda:NOW,context_assembler=Context()).tick(routine='nightly-review')
        coverage=packets[0]['calendarCoverage']
        self.assertFalse(coverage['completeAgenda']);self.assertEqual(coverage['includedMeetingRecords'],1)
        self.assertEqual(sum(coverage['includedRecordsByLocalDay'].values()),1)
        self.assertIn('Missing events are not evidence of free time',packets[0]['calendarCoverageWarning'])
        self.assertIn('never silently choose the narrative time',SYSTEM)

    def test_fresh_model_expiry_is_runtime_owned_at_completion(self):
        completed=[NOW]
        class BadTimestamp(Reasoner):
            def reason(self,packet):
                result=super().reason(packet);result['decisions'][0]['expiresAt']='invalid model timestamp';completed[0]=NOW+45000;return result
        self.ingest(event());reasoner=BadTimestamp()
        receipt=Runner(self.state,reasoner,clock=lambda:completed[0]).tick()
        self.assertEqual(receipt['decisions'][0]['expiresAt'],NOW+45000+86400000)
        self.assertEqual(reasoner.calls,1)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM runs').fetchone()[0],1)
    def test_fresh_model_can_omit_expiry_without_an_extra_call(self):
        class NoExpiry(Reasoner):
            def reason(self,packet):
                result=super().reason(packet);result['decisions'][0].pop('expiresAt');return result
        self.ingest(event());reasoner=NoExpiry()
        receipt=Runner(self.state,reasoner,clock=lambda:NOW).tick()
        self.assertEqual(receipt['decisions'][0]['expiresAt'],NOW+86400000);self.assertEqual(reasoner.calls,1)
    def test_stored_shadow_invalid_expiry_is_still_rejected(self):
        class Publisher(PolicyPublisher):
            def publish(self,d,e):raise AssertionError('invalid stored expiry must not publish')
        self.ingest(event());run=Runner(self.state,Reasoner(),clock=lambda:NOW).tick()['runId']
        record=json.loads(self.state.db.execute('SELECT result FROM runs WHERE id=?',(run,)).fetchone()[0])
        record['decisions'][0]['expiresAt']='invalid stored timestamp'
        self.state.db.execute('UPDATE runs SET result=? WHERE id=?',(json.dumps(record),run))
        with self.assertRaisesRegex(ValueError,'invalid decision expiry'):
            Runner(self.state,None,Publisher(),clock=lambda:NOW).publish_shadow(run)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_gateway_url_security(self):
        from proactive_intelligence.runner import GatewayPublisher
        for url in ('https://example.com','http://user@127.0.0.1:8787','http://127.0.0.1:8787/path','http://127.0.0.1:8787?x=1'):
            with self.assertRaises(ValueError): GatewayPublisher(url,'not-a-secret')

if __name__ == '__main__': unittest.main()
