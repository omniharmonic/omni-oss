"""The omni-scheduling skill must not contradict the proactive engine (scheduling.py, cards.SCHEDULING_RULES)."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from proactive_intelligence import scheduling  # noqa: E402
from proactive_intelligence import cards  # noqa: E402
import omni_propose  # noqa: E402

SKILL = (ROOT / "hermes/skills/omni-scheduling/SKILL.md").read_text()


class SchedulingSkillTest(unittest.TestCase):
    def test_frontmatter_description_fits_the_limit(self):
        self.assertEqual(re.search(r"^name: (.+)$", SKILL, re.M).group(1), "omni-scheduling")
        self.assertLessEqual(len(re.search(r"^description: (.+)$", SKILL, re.M).group(1)), 60)

    def test_numbers_match_the_engine(self):
        self.assertIn("%d:00 to %d:00" % (scheduling.WORK_START_H, scheduling.WORK_END_H), SKILL)
        self.assertIn("between %d and %d minutes" % (scheduling.MIN_MINUTES, scheduling.MAX_MINUTES), SKILL)
        self.assertIn("Never more than %d" % scheduling.MAX_SLOTS, SKILL)
        self.assertIn("OMNI_TIMEZONE", SKILL)
        self.assertEqual(scheduling.MAX_SLOTS, 5)
        for text in (SKILL, cards.SCHEDULING_RULES):
            self.assertIn("60 minutes", text)
            self.assertIn("urgent offers the earliest 2 or 3", text)
            self.assertIn("standard", text)
            self.assertIn("4 or 5", text)
            self.assertIn("quick check-in is 30", text)
        self.assertIn("(1 to 5)", cards.SCHEDULING_RULES)

    def test_titles_and_invite_match_the_engine(self):
        self.assertTrue(scheduling.confirmed_title("Ana", "Plan").startswith("Ana / Alex"))
        self.assertIn("HOLD: <Person> — <topic>", SKILL)
        self.assertIn("<Person> / <Owner> — <topic>", SKILL)
        self.assertIn("withMeet: true", SKILL)

    def test_never_invites_directly_and_never_shares_the_retired_link(self):
        self.assertIn("Never invite directly", SKILL)
        self.assertIn("never a booking link", SKILL)

    def test_skill_documents_the_one_based_confirm_and_the_attach_order(self):
        self.assertIn("n counts from 1", SKILL)
        self.assertIn("confirm prop_ab12cd34ef56 2", SKILL)
        self.assertIn("attach-approval", SKILL)
        self.assertLess(SKILL.index("manage_holds.py place"), SKILL.index("attach-approval"))
        self.assertIn("--thread-ref", SKILL)

    def test_a_pick_is_confirm_then_propose_then_attach_invite(self):
        i_confirm = SKILL.index("manage_holds.py confirm <prop_id> <n>")
        i_invite = SKILL.index("omni_propose.py calendar-invite")
        i_attach = SKILL.index("manage_holds.py attach-invite <prop_id> <approval_id>")
        self.assertLess(i_confirm, i_invite)
        self.assertLess(i_invite, i_attach)
        self.assertIn('"already": true', SKILL)

    def test_learning_never_edits_the_repo_skill(self):
        learning = SKILL[SKILL.index("## Learning"):]
        self.assertIn("Never edit this skill", learning)
        self.assertIn("`memory`", learning)
        self.assertIn("~/.hermes/skills", learning)
        self.assertIn("omni-scheduling-lessons", learning)
        self.assertNotIn("fix this skill", SKILL)

    def test_a_proactive_run_reads_the_rules_but_never_runs_the_commands(self):
        section = SKILL[SKILL.index("## In a proactive run"):SKILL.index("## Rules")]
        self.assertIn("never run its commands", section)
        self.assertIn("proposedSlots", section)

    def test_cli_invite_can_carry_meet(self):
        kind, payload = omni_propose._payload(omni_propose.argparse.Namespace(
            kind="calendar-invite", title="T", start="s", end="e", attendee=["a@b.co"], location=None, description=None, with_meet=True))
        self.assertTrue(payload["withMeet"])


if __name__ == "__main__":
    unittest.main()
