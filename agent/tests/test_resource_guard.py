"""omni-resource-guard: browser session cap, fan-out refusal, reaper plan, concurrency ceilings.

Offline and pure: no browser starts, nothing is killed.

    python3 -m unittest tests.test_resource_guard -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "omni_resource_guard", ROOT / "hermes/plugins/omni-resource-guard/__init__.py")
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)

sys.path.insert(0, str(ROOT / "hermes"))
import install  # noqa: E402

NORMAL, CRITICAL = 1, 4


def blocked(cmd, live=(), pressure=NORMAL):
    return g.decide_terminal(cmd, list(live), pressure) is not None


class TerminalDecision(unittest.TestCase):
    def test_unrelated_commands_pass(self):
        self.assertFalse(blocked("ls -la && git status", live=["a", "b", "c", "d"]))

    def test_first_sessions_pass(self):
        self.assertFalse(blocked("agent-browser --session flights open https://example.com"))
        self.assertFalse(blocked("agent-browser open https://example.com", live=["x", "y"]))

    def test_new_session_past_cap_refused_with_names(self):
        d = g.decide_terminal("agent-browser --session airbnb open https://airbnb.com",
                              ["pm5", "sp4", "zi2"], NORMAL)
        self.assertEqual(d["action"], "block")
        self.assertIn("pm5, sp4, zi2", d["message"])
        self.assertIn("close", d["message"])

    def test_reusing_a_live_session_past_cap_passes(self):
        self.assertFalse(blocked("agent-browser --session sp4 snapshot -i", live=["pm5", "sp4", "zi2"]))
        self.assertFalse(blocked("agent-browser --session=sp4 click @e3", live=["pm5", "sp4", "zi2"]))

    def test_close_and_list_always_pass(self):
        live = ["a", "b", "c", "d", "e", "f", "g"]
        self.assertFalse(blocked("agent-browser --session new close", live=live, pressure=CRITICAL))
        self.assertFalse(blocked("agent-browser close --all", live=live))
        self.assertFalse(blocked("agent-browser session list", live=live))
        self.assertFalse(blocked("agent-browser --help", live=live))

    def test_env_prefix_session(self):
        self.assertTrue(blocked("AGENT_BROWSER_SESSION=new agent-browser open x", live=["a", "b", "c"]))
        self.assertFalse(blocked("AGENT_BROWSER_SESSION=b agent-browser open x", live=["a", "b", "c"]))

    def test_full_binary_path_is_seen(self):
        cmd = "/opt/homebrew/Cellar/agent-browser/0.35.1/bin/agent-browser --session z open x"
        self.assertTrue(blocked(cmd, live=["a", "b", "c"]))

    # The 2026-11-08 shapes: parallel / looped sessions.
    def test_backgrounded_sessions_refused(self):
        self.assertTrue(blocked("agent-browser --session a open x & agent-browser --session b open y &"))

    def test_loop_with_variable_session_refused(self):
        self.assertTrue(blocked('for s in a b c; do agent-browser --session "$s" open "$u"; done'))

    def test_xargs_parallel_refused(self):
        self.assertTrue(blocked("cat urls | xargs -P 8 -I{} agent-browser --session s{} open {}"))

    def test_two_new_sessions_in_one_command_refused(self):
        self.assertTrue(blocked("agent-browser --session a open x && agent-browser --session b open y"))

    def test_and_and_redirect_are_not_fanout(self):
        self.assertFalse(blocked("agent-browser --session a open x 2>&1 && agent-browser --session a snapshot"))

    def test_critical_pressure_refuses_new_only(self):
        self.assertTrue(blocked("agent-browser --session n open x", pressure=CRITICAL))
        self.assertFalse(blocked("agent-browser --session a snapshot", live=["a"], pressure=CRITICAL))


class BrowserTool(unittest.TestCase):
    def test_hard_cap_and_pressure(self):
        self.assertIsNone(g.decide_browser_tool(["a"] * (g.HARD_CAP - 1), NORMAL))
        self.assertIsNotNone(g.decide_browser_tool(["a"] * g.HARD_CAP, NORMAL))
        self.assertIsNotNone(g.decide_browser_tool([], CRITICAL))


class Reaper(unittest.TestCase):
    def test_etime(self):
        self.assertEqual(g._etime_seconds("05"), 5)
        self.assertEqual(g._etime_seconds("11:14"), 674)
        self.assertEqual(g._etime_seconds("01:00:00"), 3600)
        self.assertEqual(g._etime_seconds("2-03:04:05"), 2 * 86400 + 3 * 3600 + 4 * 60 + 5)

    def test_nothing_to_reap(self):
        self.assertEqual(g.plan_reap([(1, 100), (2, 50)], NORMAL), [])

    def test_too_old_reaped(self):
        self.assertEqual(g.plan_reap([(1, g.MAX_AGE_S + 1), (2, 50)], NORMAL), [1])

    def test_past_hard_cap_oldest_first(self):
        found = [(100 + i, 600 - i) for i in range(g.HARD_CAP + 3)]  # oldest first
        self.assertEqual(g.plan_reap(found, NORMAL), [100, 101, 102])

    def test_critical_keeps_newest_only(self):
        self.assertEqual(g.plan_reap([(1, 300), (2, 200), (3, 10)], CRITICAL), [1, 2])


class Wiring(unittest.TestCase):
    def test_fails_open(self):
        with patch.object(g, "decide_terminal", side_effect=RuntimeError("boom")):
            self.assertIsNone(g._pre_tool_call("terminal", {"command": "agent-browser open x"}))

    def test_other_tools_untouched(self):
        with patch.object(g, "live_sessions", side_effect=AssertionError("must not look")):
            self.assertIsNone(g._pre_tool_call("read_file", {"path": "/x"}))
            self.assertIsNone(g._pre_tool_call("terminal", {"command": "echo hi"}))

    def test_hook_blocks_through_live_state(self):
        with patch.object(g, "live_sessions", return_value=["a", "b", "c"]), \
             patch.object(g, "memory_pressure", return_value=NORMAL):
            d = g._pre_tool_call("terminal", {"command": "agent-browser --session d open x"})
        self.assertEqual(d["action"], "block")

    def test_register_sets_idle_timeout_and_hook(self):
        hooks = []

        class Ctx:
            def register_hook(self, name, fn):
                hooks.append(name)

        with patch.dict(os.environ, {}, clear=False), patch.object(g, "_start_reaper", return_value=False):
            os.environ.pop("AGENT_BROWSER_IDLE_TIMEOUT_MS", None)
            g.register(Ctx())
            self.assertEqual(os.environ["AGENT_BROWSER_IDLE_TIMEOUT_MS"], g.IDLE_TIMEOUT_MS)
        self.assertEqual(hooks, ["pre_tool_call"])


class Limits(unittest.TestCase):
    def test_lowered_only(self):
        self.assertEqual(install.plan_limits({"delegation": {"max_concurrent_children": 10}}),
                         [("delegation.max_concurrent_children", 3)])
        self.assertEqual(install.plan_limits({}), [("delegation.max_concurrent_children", 3)])
        self.assertEqual(install.plan_limits({"delegation": {"max_concurrent_children": 3}}), [])
        self.assertEqual(install.plan_limits({"delegation": {"max_concurrent_children": 2}}), [])


if __name__ == "__main__":
    unittest.main()
