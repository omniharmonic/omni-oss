"""hermes_runtime: our scripts find Hermes' interpreter on the PM layout (v0.23+) and the legacy one.

    python3 -m unittest tests.test_hermes_runtime -v
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import hermes_runtime as hr  # noqa: E402
import install_proactive as ip  # noqa: E402


def _exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Resolve(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def launcher_printing(self, python: str):
        _exe(self.home / hr.LAUNCHER, "#!/bin/sh\n[ \"$1\" = --print-runtime-command ] && "
             "echo '%s' && exit 0\nexit 3\n" % json.dumps([python, "-I", "-c", "boot"]))

    def test_managed_layout_uses_launcher_interpreter(self):
        self.launcher_printing(sys.executable)
        self.assertEqual(hr.base_python(self.home), (sys.executable, True))
        exe, argv = hr.module_argv(self.home, "proactive_intelligence", ["--state", "s"], extra_paths=["/r/scripts"])
        self.assertEqual(exe, sys.executable)
        self.assertEqual(argv[:3], [sys.executable, "-I", "-c"])
        self.assertIn("import hermes_bootstrap", argv[3])
        self.assertIn("'/r/scripts'", argv[3])
        self.assertIn("runpy.run_module('proactive_intelligence'", argv[3])
        self.assertEqual(argv[4:], ["--state", "s"])

    def test_legacy_venv_when_no_launcher(self):
        _exe(self.home / hr.LEGACY_PYTHON, "#!/bin/sh\n")
        python, managed = hr.base_python(self.home)
        self.assertFalse(managed)
        self.assertTrue(python.endswith(hr.LEGACY_PYTHON))
        _, argv = hr.module_argv(self.home, "m", ["a"])
        self.assertNotIn("hermes_bootstrap", argv[2])

    def test_launcher_naming_a_missing_interpreter_falls_back(self):
        self.launcher_printing("/nonexistent/python3")
        _exe(self.home / hr.LEGACY_PYTHON, "#!/bin/sh\n")
        self.assertFalse(hr.base_python(self.home)[1])

    def test_no_runtime_raises(self):
        with self.assertRaises(RuntimeError):
            hr.base_python(self.home)

    def test_bootstrap_code_runs_the_module_with_our_paths(self):
        # A fake checkout with a hermes_bootstrap, and a module of ours on an extra path.
        root = self.home / "hermes-agent"
        (root).mkdir(parents=True)
        (root / "hermes_bootstrap.py").write_text("import os\nos.environ['BOOTED'] = '1'\n")
        ours = Path(tempfile.mkdtemp())
        (ours / "probe_mod.py").write_text(
            "import os, sys\nprint(os.environ.get('BOOTED'), sys.argv[1:], os.environ['HERMES_HOME'])\n")
        code = hr.bootstrap_code(self.home, "probe_mod", [str(ours)])
        env = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
        out = subprocess.run([sys.executable, "-I", "-c", code, "x", "y"], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "1 ['x', 'y'] %s" % self.home)


class Wrappers(unittest.TestCase):
    """The Hermes cron wrappers start in the repo venv and never name a versioned Hermes path."""

    def test_no_wrapper_names_the_retired_venv(self):
        repo, home = Path("/r"), Path("/h")
        for text in (ip.wrapper(repo, home, "m", "p"), ip.wrapper(repo, home, "m", "p", "events"),
                     ip.health_wrapper(repo, home), ip.memory_sync_wrapper(repo, home)):
            self.assertTrue(text.startswith("#!/r/venv/bin/python\n"), text[:40])
            self.assertNotIn("hermes-agent/venv", text)
            compile(text, "wrapper", "exec")

    def test_proactive_wrapper_resolves_hermes_at_run_time(self):
        text = ip.wrapper(Path("/r"), Path("/h"), "m", "p", "morning")
        self.assertIn("from hermes_runtime import module_argv", text)
        self.assertIn("'--routine', 'morning'", text)
        self.assertNotIn("--hermes-python", text)


if __name__ == "__main__":
    unittest.main()
