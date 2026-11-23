"""scripts/secrets.sh against a FAKE `security` command (Linux-runnable).

The fake keeps items in a JSON file and logs every argv it was called with, so
the tests can assert a secret value never appears on a command line.

    python3 tests/test_secrets_sh.py
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "secrets.sh"

FAKE_SECURITY = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    store = os.environ["FAKE_KC"]
    with open(os.environ["FAKE_KC_LOG"], "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
    items = json.load(open(store)) if os.path.exists(store) else {}
    a = sys.argv[1:]
    interactive = a[:1] == ["-i"]
    if interactive:               # `security -i`: ONE command line on stdin (never on argv)
        import shlex
        a = shlex.split(sys.stdin.readline())
    def opt(flag):
        return a[a.index(flag) + 1] if flag in a else None
    cmd = a[0]
    if cmd == "find-generic-password":
        key = opt("-s")
        if key not in items:
            print("security: SecKeychainSearchCopyNext: The specified item could not be found.", file=sys.stderr)
            sys.exit(44)
        print(items[key]["value"])
    elif cmd == "add-generic-password":
        if interactive:
            first = opt("-w")
        else:
            if a[-1] != "-w":
                sys.exit(99)      # a value on argv: refuse loudly
            first = sys.stdin.readline().rstrip("\n")[:128]   # the real tool keeps 128 characters
            second = sys.stdin.readline().rstrip("\n")[:128]
            if first != second:
                sys.exit(45)
        key = opt("-s")
        if key in items and "-U" not in a:
            sys.exit(45)
        items[key] = {"account": opt("-a"), "value": first}
        json.dump(items, open(store, "w"))
    elif cmd == "delete-generic-password":
        if opt("-s") not in items:
            sys.exit(44)
        del items[opt("-s")]
        json.dump(items, open(store, "w"))
    elif cmd == "dump-keychain":
        print('keychain: "/Users/x/Library/Keychains/login.keychain-db"')
        for k, v in items.items():
            print('attributes:')
            print('    "acct"<blob>="%s"' % v["account"])
            print('    "svce"<blob>="%s"' % k)
        print('    "svce"<blob>="omni-proton-bridge"')
    else:
        sys.exit(2)
''').lstrip()


class SecretsShTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        fake = self.tmp / "security"
        fake.write_text(FAKE_SECURITY)
        fake.chmod(0o755)
        self.env = dict(os.environ, OMNI_SECURITY_BIN=str(fake),
                        FAKE_KC=str(self.tmp / "kc.json"),
                        FAKE_KC_LOG=str(self.tmp / "argv.log"))
        self.env.pop("OMNI_SECRETS_REPO", None)

    def sh(self, *args, stdin=""):
        return subprocess.run(["bash", str(SCRIPT), *args], input=stdin, text=True,
                              capture_output=True, env=self.env)

    def argv_log(self):
        p = self.tmp / "argv.log"
        return p.read_text() if p.exists() else ""

    def test_set_then_get_value_never_on_argv(self):
        r = self.sh("--repo", "agent", "set", "TELEGRAM_BOT_TOKEN", stdin="s3cr3t-value\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("s3cr3t-value", self.argv_log())
        self.assertNotIn("s3cr3t-value", r.stdout + r.stderr)
        r = self.sh("--repo", "agent", "get", "TELEGRAM_BOT_TOKEN")
        self.assertEqual((r.returncode, r.stdout), (0, "s3cr3t-value\n"))
        items = json.loads((self.tmp / "kc.json").read_text())
        self.assertEqual(items["omni.agent.TELEGRAM_BOT_TOKEN"]["account"], "TELEGRAM_BOT_TOKEN")

    def test_long_token_is_stored_whole_and_never_on_argv(self):
        token = "eyJ" + "aB3-_." * 124          # 747 characters, JWT-shaped
        r = self.sh("--repo", "prism", "set", "PARACHUTE_TOKEN", stdin=token + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sh("--repo", "prism", "get", "PARACHUTE_TOKEN").stdout.strip(), token)
        self.assertNotIn(token[:40], self.argv_log())

    def test_setting_again_replaces_the_item(self):
        for v in ("first-value-0123456789", "second-value-0123456789"):
            self.assertEqual(self.sh("--repo", "prism", "set", "SESSION_SECRET", stdin=v + "\n").returncode, 0)
        self.assertEqual(self.sh("--repo", "prism", "get", "SESSION_SECRET").stdout.strip(), "second-value-0123456789")

    def test_long_value_with_unsafe_characters_is_refused_not_truncated(self):
        r = self.sh("--repo", "prism", "set", "ODD", stdin=("pass word \"x\" " * 20) + "\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refused", r.stderr)
        self.assertEqual(self.sh("--repo", "prism", "get", "ODD").returncode, 2)

    def test_set_refuses_empty_multiline_and_bad_names(self):
        self.assertEqual(self.sh("--repo", "agent", "set", "X_Y", stdin="").returncode, 1)
        self.assertEqual(self.sh("--repo", "agent", "set", "X_Y", stdin="a\nb\n").returncode, 1)
        self.assertEqual(self.sh("--repo", "agent", "set", "lower", stdin="v\n").returncode, 1)
        self.assertEqual(self.sh("--repo", "agent", "set", "A;rm", stdin="v\n").returncode, 1)
        self.assertEqual(self.sh("--repo", "Bad.Repo", "set", "A", stdin="v\n").returncode, 1)
        self.assertEqual(self.sh("set", "A", stdin="v\n").returncode, 1)   # no repo

    def test_get_missing_is_exit_2(self):
        r = self.sh("--repo", "prism", "get", "NOPE")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")

    def test_repo_from_env(self):
        self.env["OMNI_SECRETS_REPO"] = "prism"
        self.assertEqual(self.sh("set", "SESSION_SECRET", stdin="abc\n").returncode, 0)
        self.assertEqual(self.sh("get", "SESSION_SECRET").stdout, "abc\n")

    def test_env_file_renders_0600_and_quotes(self):
        self.sh("--repo", "prism", "set", "SESSION_SECRET", stdin="AbC+/=123\n")
        self.sh("--repo", "prism", "set", "MAGIC_FROM", stdin="Prism <p@x.y>\n")
        tmpl = self.tmp / ".env.tmpl"
        tmpl.write_text("# comment\nPORT=8787\nSESSION_SECRET=@keychain\nMAGIC_FROM=@keychain\n")
        r = self.sh("--repo", "prism", "env-file", str(tmpl))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.tmp / ".env"
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(out.read_text(),
                         "# comment\nPORT=8787\nSESSION_SECRET=AbC+/=123\nMAGIC_FROM='Prism <p@x.y>'\n")
        self.assertNotIn("AbC+/=123", self.argv_log())

    def test_env_file_missing_item_writes_nothing(self):
        tmpl = self.tmp / "agent.env.tmpl"
        tmpl.write_text("A_KEY=@keychain\n")
        out = self.tmp / "agent.env"
        out.write_text("OLD=1\n")
        r = self.sh("--repo", "agent", "env-file", str(tmpl))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(out.read_text(), "OLD=1\n")          # untouched
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith(".secrets-env")], [])

    def test_env_file_refuses_overwriting_template(self):
        tmpl = self.tmp / "plain.env"
        tmpl.write_text("A=1\n")
        self.assertEqual(self.sh("--repo", "agent", "env-file", str(tmpl)).returncode, 1)

    def test_list_names_only(self):
        self.sh("--repo", "agent", "set", "TELEGRAM_BOT_TOKEN", stdin="tok\n")
        self.sh("--repo", "prism", "set", "SESSION_SECRET", stdin="sess\n")
        r = self.sh("list")
        self.assertEqual(r.stdout.split(), ["omni.agent.TELEGRAM_BOT_TOKEN", "omni.prism.SESSION_SECRET"])
        self.assertNotIn("tok", r.stdout.replace("TOKEN", ""))
        r = self.sh("--repo", "prism", "list")
        self.assertEqual(r.stdout.split(), ["omni.prism.SESSION_SECRET"])


if __name__ == "__main__":
    unittest.main()
