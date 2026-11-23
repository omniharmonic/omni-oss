"""hermes/install.py step 3d: Hermes memory limits raised to 8000 / 4000, idempotently, other keys kept, backup first."""
import contextlib
import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

# A stand-in for `hermes config set <dotted.key> <value>`: load, set one nested key, write back (Hermes coerces ints).
FAKE_HERMES = '''#!%s
import sys, yaml
from pathlib import Path
assert sys.argv[1:3] == ['config', 'set'], sys.argv
key, value = sys.argv[3], sys.argv[4]
path = Path(sys.argv[0]).parent / 'home' / 'config.yaml'
cfg = yaml.safe_load(path.read_text()) or {}
node = cfg
for part in key.split('.')[:-1]:
    node = node.setdefault(part, {})
node[key.split('.')[-1]] = int(value) if value.lstrip('-').isdigit() else value
path.write_text(yaml.safe_dump(cfg, sort_keys=False))
with open(Path(sys.argv[0]).parent / 'calls.log', 'a') as f: f.write(' '.join(sys.argv[1:]) + '\\n')
''' % sys.executable

CONFIG = {
    'model': {'default': 'gpt-5.6-sol', 'provider': 'openai-codex'},
    'memory': {'memory_enabled': True, 'user_profile_enabled': True, 'write_approval': False,
               'memory_char_limit': 2200, 'user_char_limit': 1375, 'nudge_interval': 10, 'provider': '', 'flush_min_turns': 6},
    'skills': {'external_dirs': ['/repo/hermes/skills'], 'write_approval': False, 'creation_nudge_interval': 15},
    'plugins': {'enabled': ['omni-buzz-guard', 'omni-bridge']},
}


def load_installer():
    spec = importlib.util.spec_from_file_location('hermes_install_memory', ROOT / 'hermes/install.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class MemoryLimitTests(unittest.TestCase):
    def setUp(self):
        self.inst = load_installer()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.home = root / 'home'
        self.home.mkdir()
        self.config = self.home / 'config.yaml'
        self.config.write_text(yaml.safe_dump(CONFIG, sort_keys=False))
        self.bin = root / 'hermes'
        self.bin.write_text(FAKE_HERMES)
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.log = root / 'calls.log'

    def tearDown(self):
        self.temp.cleanup()

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.inst.main(list(argv) + ['--hermes-home', str(self.home), '--hermes-bin', str(self.bin)])
        return rc, out.getvalue()

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def backups(self):
        return sorted(p.name for p in self.home.iterdir() if p.name.startswith('config.yaml.bak-'))

    def test_the_plan_touches_only_the_two_limits_and_never_lowers(self):
        self.assertEqual(self.inst.plan_memory_limits(CONFIG), [('memory.memory_char_limit', 8000), ('memory.user_char_limit', 4000)])
        self.assertEqual(self.inst.plan_memory_limits({'memory': {'memory_char_limit': 9000, 'user_char_limit': 4000}}), [])
        self.assertEqual(self.inst.plan_memory_limits({}), [('memory.memory_char_limit', 8000), ('memory.user_char_limit', 4000)])
        self.assertEqual(self.inst.plan_memory_limits({'memory': {'memory_char_limit': '8000', 'user_char_limit': True}}),
                         [('memory.memory_char_limit', 8000), ('memory.user_char_limit', 4000)])

    def test_dry_run_is_the_default_and_changes_nothing(self):
        before = self.config.read_text()
        rc, out = self.main('--memory-only')
        self.assertEqual(rc, 0)
        self.assertIn('config set memory.memory_char_limit 8000', out)
        self.assertIn('dry run', out)
        self.assertEqual((self.config.read_text(), self.calls(), self.backups()), (before, [], []))

    def test_apply_raises_the_limits_keeps_every_other_key_and_backs_up_first(self):
        rc, out = self.main('--memory-only', '--apply')
        self.assertEqual(rc, 0)
        cfg = yaml.safe_load(self.config.read_text())
        self.assertEqual((cfg['memory']['memory_char_limit'], cfg['memory']['user_char_limit']), (8000, 4000))
        expected = yaml.safe_load(yaml.safe_dump(CONFIG))
        expected['memory'].update(memory_char_limit=8000, user_char_limit=4000)
        self.assertEqual(cfg, expected)
        [backup] = self.backups()
        self.assertEqual(yaml.safe_load((self.home / backup).read_text()), CONFIG)
        self.assertEqual(self.calls(), ['config set memory.memory_char_limit 8000', 'config set memory.user_char_limit 4000'])

    def test_a_second_apply_is_a_no_op(self):
        self.main('--memory-only', '--apply')
        rc, out = self.main('--memory-only', '--apply')
        self.assertEqual(rc, 0)
        self.assertIn('ok', out)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(len(self.backups()), 1)

    def test_the_full_install_includes_the_step(self):
        rc, out = self.main('--profile', 'dev')
        self.assertEqual(rc, 0)
        self.assertIn('3d. Hermes memory limits', out)
        self.assertIn('config set memory.user_char_limit 4000', out)


if __name__ == '__main__':
    unittest.main()
