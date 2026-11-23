"""The coordinator lock reclaims a wedged holder (stopped, gone, or over the bound) and never a live
in-bound one; the every-minute path fails visibly after a long coordinator-busy streak.

Regression for 2026-11-08: an --event-intake run SIGSTOPped by a gateway kill held the lock for 3 days."""
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from proactive_intelligence import intake  # noqa: E402
from proactive_intelligence.intake import coordinator_lock, coordinator_lock_wait, reclaim_stale, record_busy, BUSY_FAIL_AFTER  # noqa: E402

HOLDER = '''
import sys,time
from pathlib import Path
sys.path.insert(0,%r)
from proactive_intelligence.intake import coordinator_lock
with coordinator_lock(Path(sys.argv[1])) as acquired:
    print('ready' if acquired else 'busy',flush=True)
    time.sleep(120)
''' % str(SCRIPTS)


def spawn_holder(path):
    child = subprocess.Popen([sys.executable, '-c', HOLDER, str(path)], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == 'ready'
    return child


class Tests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'state.db'
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                os.kill(child.pid, signal.SIGCONT)
                child.kill()
            child.wait(10)
            child.stdout.close()
        self.dir.cleanup()

    def holder(self):
        child = spawn_holder(self.path)
        self.children.append(child)
        return child

    def test_holder_record_written_and_cleared(self):
        lock = Path(str(self.path) + '.coordinator.lock')
        with coordinator_lock(self.path) as acquired:
            self.assertTrue(acquired)
            record = json.loads(lock.read_text())
            self.assertEqual(record['pid'], os.getpid())
            self.assertLessEqual(abs(record['startedAt'] - time.time()), 5)
        self.assertEqual(lock.read_text(), '')

    def test_live_running_holder_is_never_killed(self):
        child = self.holder()
        with redirect_stderr(io.StringIO()):
            with coordinator_lock(self.path) as acquired:
                self.assertFalse(acquired)
        self.assertIsNone(child.poll())

    def test_stopped_holder_is_terminated_and_lock_taken(self):
        child = self.holder()
        os.kill(child.pid, signal.SIGSTOP)
        deadline = time.time() + 5
        while not intake.process_info(child.pid)[0].startswith('T') and time.time() < deadline:
            time.sleep(0.05)
        err = io.StringIO()
        with redirect_stderr(err):
            with coordinator_lock(self.path) as acquired:
                self.assertTrue(acquired)
        self.assertIsNotNone(child.wait(10))
        self.assertIn('stopped', err.getvalue())
        self.assertIn('lock reclaimed', err.getvalue())

    def test_holder_over_bound_is_terminated(self):
        child = self.holder()
        # backdate the holder's record rather than wait out a real bound
        lock = Path(str(self.path) + '.coordinator.lock')
        lock.write_text(json.dumps({'pid': child.pid, 'startedAt': int(time.time()) - 10}))
        err = io.StringIO()
        with redirect_stderr(err):
            with coordinator_lock(self.path, max_hold_s=5) as acquired:
                self.assertTrue(acquired)
        self.assertIsNotNone(child.wait(10))
        self.assertIn('over the 5s bound', err.getvalue())

    def test_routine_wait_also_reclaims_stopped_holder(self):
        child = self.holder()
        os.kill(child.pid, signal.SIGSTOP)
        with redirect_stderr(io.StringIO()):
            with coordinator_lock_wait(self.path, 0, 0) as acquired:
                self.assertTrue(acquired)
        self.assertIsNotNone(child.wait(10))

    def test_reclaim_decisions_with_fakes(self):
        fd = os.open(str(self.path) + '.coordinator.lock', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        kills = []
        kill = lambda pid, sig: kills.append(sig)
        now = lambda: 10_000

        def write(pid, started):
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps({'pid': pid, 'startedAt': started}).encode(), 0)

        # no holder record: never guess
        self.assertFalse(reclaim_stale(fd, 900, info=lambda p: ('T', 5), kill=kill, now=now))
        # running and in bound: untouched
        write(4242, 9_500)
        self.assertFalse(reclaim_stale(fd, 900, info=lambda p: ('S', 1000), kill=kill, now=now))
        # pid reused by a younger process: untouched even though it is over the bound
        write(4242, 1_000)
        self.assertFalse(reclaim_stale(fd, 900, info=lambda p: ('T', 30), kill=kill, now=now))
        # ps failed: untouched
        self.assertFalse(reclaim_stale(fd, 900, info=lambda p: 'unknown', kill=kill, now=now))
        self.assertEqual(kills, [])
        # stopped: SIGCONT then SIGTERM, then SIGKILL when it will not die
        write(4242, 9_990)
        with redirect_stderr(io.StringIO()):
            reclaim_stale(fd, 900, info=lambda p: ('T', 20), kill=kill, sleep=lambda s: None, now=now)
        self.assertEqual(kills, [signal.SIGCONT, signal.SIGTERM, signal.SIGKILL])

    def test_etime_parsing(self):
        self.assertEqual(intake._etime_seconds('05:07'), 307)
        self.assertEqual(intake._etime_seconds('01:00:00'), 3600)
        self.assertEqual(intake._etime_seconds('3-02:00:01'), 3 * 86400 + 7201)

    def test_busy_streak_fails_visibly_then_resets(self):
        import proactive_intelligence.__main__ as cli
        with patch.object(sys, 'argv', ['proactive', '--state', str(self.path)]), patch.object(cli, '_main') as work:
            with coordinator_lock(self.path):
                for i in range(BUSY_FAIL_AFTER - 1):
                    with redirect_stdout(io.StringIO()) as out:
                        cli.main()
                    self.assertEqual(json.loads(out.getvalue())['consecutiveBusy'], i + 1)
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as exit_:
                    cli.main()
                self.assertEqual(exit_.exception.code, 1)
                self.assertIn('consecutive runs', err.getvalue())
            work.assert_not_called()
            cli.main()
            work.assert_called_once()
        self.assertEqual(record_busy(self.path, True), 1)


if __name__ == '__main__':
    unittest.main()
