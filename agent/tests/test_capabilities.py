"""The capability manifest: one file, rendered per surface (config/omni-capabilities.yaml)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import gen_capabilities as G  # noqa: E402


class TestCapabilities(unittest.TestCase):
    def test_render_omni_app_mentions_browser_and_approval(self):
        out = G.render("omni-app")
        self.assertIn("browser", out)
        self.assertIn("approval", out)

    def test_render_buzz_omits_terminal(self):
        out = G.render("buzz")
        self.assertNotIn("terminal", out.lower())
        self.assertIn("Search and read the Parachute vault", out)

    def test_every_capability_has_valid_gate_and_surfaces(self):
        self.assertEqual(G.check(G.load()), [])
        for c in G.load()["capabilities"]:
            self.assertIn(c["gate"], G.GATES)
            self.assertTrue(set(c["surfaces"]) <= set(G.SURFACES))

    def test_check_flags_bad_gate(self):
        bad = {"capabilities": [{"id": "x", "label": "l", "how": "h", "gate": "maybe", "surfaces": ["omni-app"]}], "limits": []}
        self.assertTrue(G.check(bad))

    def test_vault_delete_is_an_ask_first_instruction_not_a_claimed_block(self):
        row = next(c for c in G.load()["capabilities"] if c["id"] == "vault-bulk-delete")
        self.assertEqual(row["gate"], "ask")
        for surface in row["surfaces"]:
            out = G.render(surface)
            ask = out.index("Ask the owner first")
            self.assertGreater(out.index("Delete or bulk-rewrite vault content"), ask, surface)
            never = out.find("Never, on any surface:")
            self.assertTrue(never == -1 or out.index("Delete or bulk-rewrite vault content") < never, surface)

    def test_bridge_prompt_uses_manifest(self):
        from test_omni_policy import B
        block = G.render("omni-app")
        self.assertIn(block, B.INTERNAL_GUIDE)
        self.assertIn("omni_propose", B.INTERNAL_GUIDE)
        self.assertIn("never send through a browser", B.INTERNAL_GUIDE)
        # The stricter guarded mode keeps its hand-written wording.
        self.assertNotIn(block.split("\n")[0], B.GUIDE)
        self.assertIn(B._TOOLS_SENTENCE, B.GUIDE)

    def test_bridge_falls_back_when_manifest_missing(self):
        from test_omni_policy import B
        with self.assertLogs(B.logger, "WARNING"):
            text = B._capability_block(manifest=REPO / "nope.yaml")
        self.assertEqual(text, B._TOOLS_SENTENCE)


if __name__ == "__main__":
    unittest.main()
