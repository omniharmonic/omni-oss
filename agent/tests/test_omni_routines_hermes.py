"""Hermes routines: the tweet run publishes its draft into its own Omni chat (approval only for a
1–280 character draft), briefings publish nothing, routine runs skip the nudge suppression check,
and a routine waits for the coordinator lock before failing visibly."""
import fcntl
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import omni_routine  # noqa: E402
from proactive_intelligence.state import State  # noqa: E402
from proactive_intelligence.runner import Runner, GatewayPublisher  # noqa: E402
from proactive_intelligence.routines import TASKS, tweet_parts, routine_day  # noqa: E402
from proactive_intelligence.intake import coordinator_lock, coordinator_lock_wait  # noqa: E402

NOW = 1791720000000
TOKEN = 'test-service-secret-0123456789'
DRAFT = 'User-owned AI starts with owning your notes.'
RATIONALE = 'Grounded in this week\'s public post about local-first notes.'


def routine_event(routine='tweet', day='2026-10-17'):
    return dict(id='profile', revision='r1', kind='reply-owed', entity='profile', occurred_at=NOW,
                content='Routing anchor only.', url='parachute://profile',
                routine=routine, boundary='routine:%s:%s' % (routine, day))


class Reasoner:
    def __init__(self, **decision):
        self.decision = decision

    def reason(self, packet):
        base = dict(sourceId='profile', sourceRevision='r1', action='surface', title='Tweet idea',
                    summary='One public-safe idea', body=RATIONALE, evidence=[{'sourceId': 'profile', 'revision': 'r1'}])
        base.update(self.decision)
        return {'decisions': [base]}


class RoutinePublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / 'state.db')
        self.env = patch.dict(os.environ, {'OMNI_ROUTINES_PRIMARY': '1'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.state.db.close()
        self.temp.cleanup()

    def run_routine(self, routine='tweet', publish=None, **decision):
        event = routine_event(routine)
        self.state.ingest('routine:' + routine, event['boundary'], [event], now=NOW, coalesce_ms=0)
        publisher = GatewayPublisher('http://127.0.0.1:8787', TOKEN, names=lambda: [])
        calls = []

        def fake(*a, **kw):
            calls.append((a, kw))
            if publish:
                return publish(*a, **kw)
            return {'ok': True, 'thread_id': 'omni-routine-tweet-2026-10-17', 'post_id': a[3], 'replayed': False, 'approval_id': 'apr_1'}
        with patch.object(omni_routine, 'publish_routine', side_effect=fake), \
             patch.object(GatewayPublisher, 'suppressed', side_effect=AssertionError('routine runs never ask about suppression')):
            receipt = Runner(self.state, Reasoner(**decision), publisher, clock=lambda: NOW).tick(
                live=True, focus_boundary=event['boundary'], routine=routine)
        return receipt, calls

    def test_a_valid_draft_becomes_one_post_with_a_tweet_approval_and_a_push(self):
        receipt, calls = self.run_routine(draft=DRAFT)
        self.assertEqual(receipt['publications'], 1)
        args, kw = calls[0]
        self.assertEqual(args, ('tweet', '2026-10-17', 'Tweet draft · Sat 17 Oct', 'tweet-2026-10-17-1',
                                RATIONALE + '\n\n---\n\n' + DRAFT, True,
                                {'kind': 'tweet', 'payload': {'text': DRAFT}, 'summary': DRAFT[:80]}))
        self.assertEqual(kw, {'base': 'http://127.0.0.1:8787', 'token': TOKEN})

    def test_no_draft_still_publishes_the_rationale_with_a_push_and_no_approval(self):
        receipt, calls = self.run_routine(action='ignore')
        args, _ = calls[0]
        self.assertTrue(args[4].startswith(RATIONALE))
        self.assertTrue(args[4].endswith('(No publishable tweet this time.)'))
        self.assertIs(args[5], True)
        self.assertIsNone(args[6])

    def test_an_over_long_draft_gets_no_approval_but_is_shown_never_called_no_tweet(self):
        _, calls = self.run_routine(draft='x' * 281)
        self.assertIsNone(calls[0][0][6])
        body = calls[0][0][4]
        self.assertNotIn('No publishable tweet', body)
        self.assertIn('281 characters', body)
        self.assertTrue(body.endswith('x' * 281))

    def test_an_over_long_draft_gets_one_rewrite_and_the_rewrite_is_published(self):
        drafts = ['y' * 290, DRAFT]
        packets = []

        class Rewriter(Reasoner):
            def reason(self, packet):
                packets.append(packet)
                self.decision = {'draft': drafts[len(packets) - 1]}
                return Reasoner.reason(self, packet)
        event = routine_event()
        self.state.ingest('routine:tweet', event['boundary'], [event], now=NOW, coalesce_ms=0)
        calls = []
        with patch.object(omni_routine, 'publish_routine', side_effect=lambda *a, **kw: calls.append(a) or {'ok': True}):
            Runner(self.state, Rewriter(), GatewayPublisher('http://127.0.0.1:8787', TOKEN, names=lambda: []),
                   clock=lambda: NOW).tick(live=True, focus_boundary=event['boundary'], routine='tweet')
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[1]['rewrite'], {'draft': 'y' * 290, 'characters': 290})
        self.assertIn('290 characters', packets[1]['task'])
        self.assertEqual(calls[0][6]['payload'], {'text': DRAFT})

    def test_a_draft_within_the_limit_is_not_rewritten(self):
        packets = []

        class Counting(Reasoner):
            def reason(self, packet):
                packets.append(packet)
                return Reasoner.reason(self, packet)
        event = routine_event()
        self.state.ingest('routine:tweet', event['boundary'], [event], now=NOW, coalesce_ms=0)
        with patch.object(omni_routine, 'publish_routine', return_value={'ok': True}):
            Runner(self.state, Counting(draft=DRAFT), GatewayPublisher('http://127.0.0.1:8787', TOKEN, names=lambda: []),
                   clock=lambda: NOW).tick(live=True, focus_boundary=event['boundary'], routine='tweet')
        self.assertEqual(len(packets), 1)

    def test_recent_drafts_are_the_latest_finished_tweet_drafts_newest_first(self):
        for i, draft in enumerate(['old idea', '', 'newer idea', 'newest idea']):
            self.state.db.execute("INSERT INTO runs(id,day,bucket,status,started,result,routine) VALUES(?,?,?,?,?,?,?)",
                                  ('r%d' % i, '2026-10-1%d' % i, 'ordinary', 'done', NOW + i,
                                   json.dumps({'decisions': [{'draft': draft}]}), 'tweet'))
        self.state.db.execute("INSERT INTO runs(id,day,bucket,status,started,result,routine) VALUES('f','d','ordinary','failed',?,?,'tweet')",
                              (NOW + 9, json.dumps({'decisions': [{'draft': 'failed run'}]})))
        self.assertEqual(self.state.recent_drafts('tweet', 2), ['newest idea', 'newer idea'])

    def test_the_tweet_task_points_at_recent_drafts_and_a_240_target(self):
        self.assertIn('recent_drafts', TASKS['tweet'])
        self.assertIn('240', TASKS['tweet'])

    def test_a_publish_failure_is_counted_so_the_job_fails_visibly(self):
        def fail(*a, **kw):
            raise omni_routine.RoutinePublishError('routine post failed after 3 attempts: gateway answered 503')
        receipt, _ = self.run_routine(publish=fail, draft=DRAFT)
        self.assertEqual(receipt['routineFailures'], 1)
        from proactive_intelligence.__main__ import exit_code
        self.assertEqual(exit_code(receipt), 1)
        self.assertEqual(exit_code({'wakeAgent': True}), 0)

    def test_a_wait_decision_on_the_tweet_routine_is_an_ignore_with_no_watcher(self):
        """A watcher's wake has no day boundary, so it could never publish: no watcher, just the
        rationale, without an approval (the no-draft path)."""
        receipt, calls = self.run_routine(action='wait', watch={'kind': 'time', 'at': NOW + 3600000}, draft=DRAFT)
        self.assertEqual(receipt.get('routineFailures', 0), 0)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM watchers').fetchone()[0], 0)
        args, _ = calls[0]
        self.assertTrue(args[4].startswith(RATIONALE))
        self.assertTrue(args[4].endswith('(No publishable tweet this time.)'))
        self.assertIsNone(args[6])
        stored = json.loads(self.state.db.execute("SELECT payload FROM outbox").fetchone()[0])
        self.assertEqual(stored['decision']['action'], 'ignore')

    def test_a_wait_decision_on_an_item_still_plants_a_watcher(self):
        item = dict(id='a', revision='r1', kind='task', entity='a', occurred_at=NOW, content='Item.', url='parachute://a')
        self.state.ingest('reconcile', None, [item], now=NOW, coalesce_ms=0)
        Runner(self.state, Reasoner(sourceId='a', evidence=[{'sourceId': 'a', 'revision': 'r1'}], action='wait',
                                    watch={'kind': 'time', 'at': NOW + 3600000}), None, clock=lambda: NOW).tick()
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM watchers').fetchone()[0], 1)

    def test_morning_and_evening_routines_publish_nothing_to_omni(self):
        for routine in ('morning', 'evening'):
            # A fresh state per routine: the same anchor revision would not queue a second run.
            self.state.db.close()
            self.state = State(Path(self.temp.name) / ('%s.db' % routine))
            # Composer-owned: no routine slot at all (state.BUDGETED_ROUTINES), so no run and no outbox row ...
            receipt, calls = self.run_routine(routine=routine)
            self.assertEqual(calls, [], routine)
            self.assertFalse(receipt['wakeAgent'], routine)
            self.assertEqual(self.state.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)
            # ... and a row that reaches the publisher anyway is skipped, never sent to Omni.
            with patch.object(omni_routine, 'publish_routine', side_effect=AssertionError(routine)):
                self.assertEqual(GatewayPublisher('http://127.0.0.1:8787', TOKEN).publish({}, {'routine': routine})['skipped'],
                                 'briefing-owned-by-composer')

class RoutineDismissalTests(unittest.TestCase):
    """A dismissal recorded for the routing anchor (the old code recorded one when the owner archived
    the shared nudge chat) must never silence a routine; item dismissals still suppress items."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / 'state.db')
        self.env = patch.dict(os.environ, {'OMNI_ROUTINES_PRIMARY': '1'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.state.db.close()
        self.temp.cleanup()

    def tick(self, event, reasoner, suppressed=()):
        publisher = GatewayPublisher('http://127.0.0.1:8787', TOKEN, names=lambda: [])
        calls = []

        def fake(*a, **kw):
            calls.append((a, kw))
            return {'ok': True, 'post_id': a[3], 'replayed': False}
        with patch.object(omni_routine, 'publish_routine', side_effect=fake), \
             patch.object(GatewayPublisher, 'suppressed', return_value=list(suppressed)), \
             patch.object(GatewayPublisher, 'publish_legacy', side_effect=lambda d, e: calls.append(('legacy', e['id'])) or {'ok': True}):
            receipt = Runner(self.state, reasoner, publisher, clock=lambda: NOW).tick(
                live=True, focus_boundary=event.get('boundary'), routine=event.get('routine'))
        return receipt, calls

    def test_an_old_anchor_dismissal_does_not_silence_the_tweet_routine(self):
        self.state.dismiss('profile', 'r1')  # as the old code did when the shared nudge chat was archived
        event = routine_event()
        self.state.ingest('routine:tweet', event['boundary'], [event], now=NOW, coalesce_ms=0)
        receipt, calls = self.tick(event, Reasoner(draft=DRAFT))
        self.assertEqual(receipt['publications'], 1)
        self.assertEqual(calls[0][0][6]['payload']['text'], DRAFT)

    def test_an_item_dismissal_of_the_anchor_does_not_drop_a_pending_tweet(self):
        event = routine_event()
        self.state.ingest('routine:tweet', event['boundary'], [event], now=NOW, coalesce_ms=0)
        publisher = GatewayPublisher('http://127.0.0.1:8787', TOKEN, names=lambda: [])
        with patch.object(omni_routine, 'publish_routine', side_effect=RuntimeError('gateway down')), \
             patch.object(GatewayPublisher, 'suppressed', side_effect=AssertionError('not asked')):
            Runner(self.state, Reasoner(draft=DRAFT), publisher, clock=lambda: NOW).tick(
                live=True, focus_boundary=event['boundary'], routine='tweet')
        self.state.dismiss('profile', 'r1')  # e.g. a card about the profile note itself was dismissed
        status = self.state.db.execute("SELECT status FROM outbox WHERE json_extract(payload,'$.event.routine')='tweet'").fetchone()[0]
        self.assertEqual(status, 'pending')

    def test_a_dismissed_item_is_still_suppressed(self):
        item = dict(id='a', revision='r1', kind='task', entity='a', occurred_at=NOW, content='Item.', url='parachute://a')
        self.state.dismiss('a', 'r1')
        self.state.ingest('reconcile', None, [item], now=NOW, coalesce_ms=0)
        receipt, calls = self.tick(item, Reasoner(sourceId='a'))
        self.assertFalse(receipt['wakeAgent'])
        self.assertEqual(calls, [])
        self.assertIsNone(self.state.db.execute("SELECT 1 FROM queue WHERE status='queued'").fetchone())


class RoutinesPrimaryOffTests(unittest.TestCase):
    """OMNI_ROUTINES_PRIMARY unset: Telegram's tweet job owns tweets, so Hermes' tweet routine publishes
    nothing to Omni (and never falls back to the legacy nudge path). Deploy and rollback are one switch."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / 'state.db')
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop('OMNI_ROUTINES_PRIMARY', None)

    def tearDown(self):
        self.env.stop()
        self.state.db.close()
        self.temp.cleanup()

    def test_the_publisher_skips_a_tweet_and_never_falls_back(self):
        event = routine_event()
        self.state.ingest('routine:tweet', event['boundary'], [event], now=NOW, coalesce_ms=0)
        publisher = GatewayPublisher('http://127.0.0.1:8787', TOKEN, names=lambda: [])
        with patch.object(omni_routine, 'publish_routine', side_effect=AssertionError('no Omni post with the flag off')), \
             patch.object(GatewayPublisher, 'publish_legacy', side_effect=AssertionError('no legacy fallback')), \
             patch.object(GatewayPublisher, 'publish_card', side_effect=AssertionError('no card')):
            receipt = Runner(self.state, Reasoner(draft=DRAFT), publisher, clock=lambda: NOW).tick(
                live=True, focus_boundary=event['boundary'], routine='tweet')
        self.assertEqual(receipt.get('routineFailures', 0), 0)
        from proactive_intelligence.__main__ import exit_code
        self.assertEqual(exit_code(receipt), 0)
        self.assertEqual(GatewayPublisher('http://127.0.0.1:8787', TOKEN, names=lambda: []).publish({}, event),
                         {'skipped': 'routines-primary-off', 'routine': 'tweet'})

    def test_the_tweet_job_exits_0_with_a_routines_primary_off_receipt_before_any_work(self):
        import proactive_intelligence.__main__ as cli
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sub' / 'state.db'
            out = io.StringIO()
            argv = ['proactive', '--state', str(path), '--routine', 'tweet', '--reason', '--live']
            with patch.object(sys, 'argv', argv), patch.dict(os.environ, {'OMNI_SERVICE_TOKEN': TOKEN}), \
                 patch.object(cli, 'State', side_effect=AssertionError('no state work')), redirect_stdout(out):
                cli._main()
            receipt = json.loads(out.getvalue())
            self.assertEqual(receipt['mode'], 'routines-primary-off')
            self.assertEqual(receipt['routine'], 'tweet')
            self.assertFalse(receipt['wakeAgent'])
            self.assertIn('routines-primary off', receipt['note'])

    def test_with_the_flag_on_the_tweet_job_does_its_work(self):
        import proactive_intelligence.__main__ as cli
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            argv = ['proactive', '--state', str(path), '--routine', 'tweet', '--reason', '--live']
            with patch.object(sys, 'argv', argv), patch.dict(os.environ, {'OMNI_ROUTINES_PRIMARY': '1', 'OMNI_SERVICE_TOKEN': TOKEN}), \
                 patch.object(cli, 'State', side_effect=RuntimeError('reached the work')), redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError) as caught:
                    cli._main()
            self.assertEqual(str(caught.exception), 'reached the work')


class Guard:
    """A source-health guard that holds every citation (or none)."""
    def __init__(self, hold):
        self.hold = hold
        self.matrix = {'healthy': not hold}

    def refresh(self):
        pass

    def filter(self, events):
        return {'eligible': [] if self.hold else list(events)}


class HeldOrStaleTweetTests(unittest.TestCase):
    """A tweet run that publishes nothing because its post is held or stale fails visibly."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / 'state.db')
        self.env = patch.dict(os.environ, {'OMNI_ROUTINES_PRIMARY': '1'})
        self.env.start()
        event = routine_event()
        self.state.ingest('routine:tweet', event['boundary'], [event], now=NOW, coalesce_ms=0)
        self.event = json.loads(self.state.db.execute("SELECT payload FROM evidence WHERE id='profile'").fetchone()[0])

    def tearDown(self):
        self.env.stop()
        self.state.db.close()
        self.temp.cleanup()

    def queue_post(self, expires):
        decision = dict(sourceId='profile', sourceRevision='r1', action='surface', title='t', summary='s', body=RATIONALE,
                        draft=DRAFT, evidence=[{'sourceId': 'profile', 'revision': 'r1'}], expiresAt=expires)
        self.state.db.execute("INSERT INTO outbox(id,payload,status) VALUES(?,?,?)",
                              ('k1', json.dumps({'decision': decision, 'event': self.event}), 'pending'))

    def runner(self, hold=False):
        return Runner(self.state, None, GatewayPublisher('http://127.0.0.1:8787', TOKEN, names=lambda: []), clock=lambda: NOW,
                      source_health_guard=Guard(hold))

    def test_a_stale_tweet_post_fails_the_run_with_a_reason(self):
        self.queue_post(NOW - 1)
        r = self.runner()
        with patch.object(omni_routine, 'publish_routine', side_effect=AssertionError('nothing to publish')):
            r.flush(routine='tweet')
        self.assertEqual(r.routine_failures, 1)
        self.assertTrue(any('stale' in why for why in r.routine_failure_reasons), r.routine_failure_reasons)

    def test_a_held_tweet_post_fails_the_tweet_run_but_not_the_events_job(self):
        self.queue_post(NOW + 3600000)
        events_job = self.runner(hold=True)
        with patch.object(omni_routine, 'publish_routine', side_effect=AssertionError('held')):
            events_job.flush()
            self.assertEqual(events_job.routine_failures, 0)
            tweet_run = self.runner(hold=True)
            tweet_run.flush(routine='tweet')
        self.assertEqual(tweet_run.routine_failures, 1)
        self.assertTrue(any('held' in why for why in tweet_run.routine_failure_reasons), tweet_run.routine_failure_reasons)
        self.assertEqual(self.state.db.execute("SELECT status FROM outbox WHERE id='k1'").fetchone()[0], 'pending')

    def test_the_cli_exits_non_zero_and_says_why(self):
        import proactive_intelligence.__main__ as cli
        receipt = {'wakeAgent': False, 'publications': 0, 'routineFailures': 1, 'routineFailureReasons': ['tweet: held']}
        self.assertEqual(cli.exit_code(receipt), 1)


class TweetPartsTests(unittest.TestCase):
    def test_parts_and_day(self):
        self.assertEqual(tweet_parts({'body': ' Why. ', 'draft': ' Hi '}), ('Hi', 'Why.'))
        self.assertEqual(tweet_parts({'body': 'Why.', 'draft': '   '}), (None, 'Why.'))
        self.assertEqual(tweet_parts({'body': 'Why.'}), (None, 'Why.'))
        self.assertEqual(tweet_parts({'body': 'Why.', 'draft': 'y' * 280})[0], 'y' * 280)
        self.assertEqual(routine_day({'boundary': 'routine:tweet:2026-10-17'}), '2026-10-17')
        with self.assertRaises(ValueError):
            routine_day({'boundary': 'change'})

    def test_the_tweet_task_asks_for_rationale_in_body_and_draft_in_draft(self):
        self.assertIn('rationale in body', TASKS['tweet'])
        self.assertIn('draft', TASKS['tweet'])
        self.assertIn('280', TASKS['tweet'])


class LockWaitTests(unittest.TestCase):
    def test_a_routine_waits_for_the_lock_then_fails_visibly(self):
        import proactive_intelligence.__main__ as cli
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            out, err = io.StringIO(), io.StringIO()
            with patch.object(sys, 'argv', ['proactive', '--state', str(path), '--routine', 'tweet']), \
                 patch.object(cli, '_main') as work, patch.object(cli, 'ROUTINE_LOCK_WAIT_S', 0.05), \
                 patch.object(cli, 'ROUTINE_LOCK_POLL_S', 0.01), redirect_stdout(out), redirect_stderr(err):
                with coordinator_lock(path):
                    with self.assertRaises(SystemExit) as stopped:
                        cli.main()
            self.assertEqual(stopped.exception.code, 1)
            work.assert_not_called()
            self.assertIn('routine-skipped: coordinator busy', err.getvalue())
            self.assertEqual(json.loads(out.getvalue())['error'], 'routine-skipped: coordinator busy')

    def test_a_routine_gets_the_lock_when_it_frees_during_the_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            fd = os.open(str(path) + '.coordinator.lock', os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            sleeps = []

            def sleep(seconds):
                sleeps.append(seconds)
                fcntl.flock(fd, fcntl.LOCK_UN)
            try:
                with coordinator_lock_wait(path, 10, 2, sleep=sleep) as acquired:
                    self.assertTrue(acquired)
            finally:
                os.close(fd)
            self.assertEqual(sleeps, [2])

    def test_a_routine_that_gets_the_lock_runs(self):
        import proactive_intelligence.__main__ as cli
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            with patch.object(sys, 'argv', ['proactive', '--state', str(path), '--routine', 'tweet']), \
                 patch.object(cli, '_main') as work, redirect_stdout(io.StringIO()):
                cli.main()
            work.assert_called_once()


if __name__ == '__main__':
    unittest.main()
