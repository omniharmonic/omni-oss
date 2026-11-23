"""system_path: cron workers get the standard tool directories back, appended."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import system_path  # noqa: E402


class SystemPathTest(unittest.TestCase):
    def test_appends_only_what_is_missing(self):
        env = {"PATH": "/hermes/bin:/usr/bin"}
        system_path.ensure(env)
        parts = env["PATH"].split(os.pathsep)
        self.assertEqual(parts[:2], ["/hermes/bin", "/usr/bin"], "the caller's order still wins")
        for d in system_path.STANDARD:
            self.assertEqual(parts.count(d), 1, d)

    def test_empty_or_missing_path(self):
        env = {}
        system_path.ensure(env)
        self.assertEqual(env["PATH"].split(os.pathsep), list(system_path.STANDARD))

    def test_idempotent(self):
        env = {"PATH": "/x"}
        once = system_path.ensure(env)
        self.assertEqual(system_path.ensure(env), once)

    def test_entry_points_call_it(self):
        root = Path(__file__).resolve().parent.parent / "scripts"
        for f in ("proactive_intelligence/__main__.py", "omni_health.py"):
            self.assertIn("system_path.ensure()", (root / f).read_text(), f)


if __name__ == "__main__":
    unittest.main()
