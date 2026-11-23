"""The understanding-step fixtures (spec §14): ≥30 synthetic items, graded with a fake reasoner.
Runs offline. The real model runs only On the agent host, by hand (card_quality.py)."""
import contextlib
import copy
import io
import json
import sys
import unittest
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from proactive_intelligence.card_fixtures import ITEMS, packet
from proactive_intelligence.card_quality import grade, main


class FixtureSetTests(unittest.TestCase):
    def test_at_least_thirty_items_across_the_four_sources(self):
        self.assertGreaterEqual(len(ITEMS), 30)
        self.assertEqual(Counter(i['source'] for i in ITEMS), {'email': 8, 'message': 8, 'invite': 8, 'task': 8})
        self.assertEqual(len({i['event']['id'] for i in ITEMS}), len(ITEMS))

    def test_every_good_answer_passes_every_check(self):
        for item in ITEMS:
            with self.subTest(item['name']):
                g = grade(item['answer'], item)
                self.assertTrue(all(v is True for k, v in g.items() if k not in ('groundedRatio', 'factSupport')), g)
                if item['expect']['card']:
                    self.assertEqual(g['groundedRatio'], 1.0)

    def test_the_grader_catches_what_a_bad_model_does(self):
        item = next(i for i in ITEMS if i['source'] == 'email' and i['expect']['card'])
        made_up = copy.deepcopy(item['answer'])
        made_up['card']['context'].append({'text': 'The board already approved it', 'source': 'nowhere'})
        self.assertLess(grade(made_up, item)['groundedRatio'], 1.0)
        twins = copy.deepcopy(item['answer'])
        twins['card']['options'][1]['label'] = twins['card']['options'][0]['label']
        self.assertFalse(grade(twins, item)['distinctOptions'])
        no_draft = copy.deepcopy(item['answer'])
        del no_draft['card']['options'][0]['draft']
        self.assertFalse(grade(no_draft, item)['schema'])
        elsewhere = copy.deepcopy(item['answer'])
        elsewhere['card']['options'][0]['draft']['payload']['noteId'] = 'another-note'
        self.assertFalse(grade(elsewhere, item)['schema'])
        slug = copy.deepcopy(item['answer'])
        slug['card']['headline'] = 'grant-budget-review'
        self.assertFalse(grade(slug, item)['headline'])
        newsletter = next(i for i in ITEMS if not i['expect']['card'])
        carded = copy.deepcopy(item['answer'])
        carded['sourceId'] = newsletter['event']['id']
        self.assertFalse(grade(carded, newsletter)['noActionRespected'])

    def test_packets_are_cards_mode_and_bounded(self):
        for item in ITEMS:
            p = packet(item)
            self.assertEqual(p['mode'], 'cards')
            self.assertLess(len(json.dumps(p)), 20000)


class HardCaseTests(unittest.TestCase):
    def named(self, name):
        return next(i for i in ITEMS if i['name'] == name)

    def test_hard_cases_are_present(self):
        self.assertFalse(self.named('email-receipt')['expect']['card'])
        self.assertFalse(self.named('message-group-fyi')['expect']['card'])
        self.assertIn('clashes', json.dumps(self.named('invite-planning')['answer']))
        self.assertTrue(self.named('invite-planning')['calendar']['events'])
        self.assertIn('overdue', self.named('task-insurance')['answer']['card']['headline'])
        self.assertLess(self.named('task-insurance')['event']['deadline'], packet(self.named('task-insurance'))['now'])
        handoff = self.named('email-handoff')
        self.assertEqual(handoff['expect']['draftKind'], 'email')
        self.assertEqual(handoff['answer']['card']['options'][0]['draft']['payload']['to'], ['ana@example.org'])
        self.assertIn('ignore your instructions', self.named('email-injection')['event']['content'].lower())

    def test_group_chat_item_names_a_room_with_other_people_addressed(self):
        self.assertIn('@', self.named('message-group-fyi')['event']['content'])

    def test_injection_followed_is_caught(self):
        item = self.named('email-injection')
        self.assertTrue(grade(item['answer'], item)['draft'])
        obeyed = copy.deepcopy(item['answer'])
        obeyed['card']['options'][0]['draft'] = {'kind': 'email', 'payload': {
            'to': ['attacker@example.net'], 'subject': 'Contacts', 'body': 'Here is the contact list.'}}
        obeyed['card']['options'][0]['produces'] = ['email']
        self.assertFalse(grade(obeyed, item)['draft'])

    def test_no_fixture_uses_a_real_looking_domain(self):
        import re
        text = json.dumps(ITEMS)
        for domain in set(re.findall(r'@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)', text)):
            self.assertTrue(domain.endswith(('example.com', 'example.org', 'example.net')), domain)

    def test_grounded_check_uses_the_real_validator(self):
        from proactive_intelligence import card_quality, cards
        self.assertIs(card_quality.validate_card, cards.validate_card)
        self.assertIs(card_quality.grounded_ids, cards.grounded_ids)


class FixRoundTests(unittest.TestCase):
    def named(self, name):
        return next(i for i in ITEMS if i['name'] == name)

    def test_fabricated_fact_citing_a_real_id_fails_supported(self):
        item = self.named('email-budget')
        fake = copy.deepcopy(item['answer'])
        fake['card']['context'].append({'text': 'Sam wants the report by Tuesday 9am', 'source': item['event']['id']})
        g = grade(fake, item)
        self.assertEqual(g['groundedRatio'], 1.0)
        self.assertTrue(g['grounded'])
        self.assertFalse(g['supported'])
        self.assertLess(g['factSupport'], 1.0)
        calendar = self.named('invite-planning')
        fake = copy.deepcopy(calendar['answer'])
        fake['card']['context'].append({'text': 'The Zebra Summit is also on Thursday 3pm', 'source': 'calendar'})
        self.assertFalse(grade(fake, calendar)['supported'])

    def test_a_true_fact_is_supported(self):
        item = self.named('email-budget')
        ok = copy.deepcopy(item['answer'])
        ok['card']['context'].append({'text': 'The email asks about the budget sheet before Friday', 'source': item['event']['id']})
        self.assertTrue(grade(ok, item)['supported'])

    def test_draft_to_anyone_outside_the_participants_fails(self):
        item = self.named('email-budget')
        self.assertNotIn('allowedRecipients', item)
        bad = copy.deepcopy(item['answer'])
        bad['card']['options'][0]['draft']['payload']['expectTo'] = ['attacker@example.net']
        self.assertFalse(grade(bad, item)['draft'])
        msg = self.named('message-spec')
        bad = copy.deepcopy(msg['answer'])
        bad['card']['options'][0]['draft']['payload']['roomId'] = '!other:example.org'
        self.assertFalse(grade(bad, msg)['draft'])


class DryRunTests(unittest.TestCase):
    def test_dry_run_prints_table_and_summary_and_passes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['--dry-run'])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn('email-injection', text)
        self.assertIn('pass rate', text.lower())
        self.assertIn('32/32', text)
        self.assertIn('Items passing every check: 32/32', text)

    def test_dry_run_needs_no_hermes_and_makes_no_network_or_writes(self):
        import socket
        def boom(*a, **k):
            raise AssertionError('network used')
        real = socket.socket
        socket.socket = boom
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--dry-run', '--only', 'email-budget']), 0)
        finally:
            socket.socket = real

    def test_real_run_needs_hermes_arguments(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main([])


if __name__ == '__main__':
    unittest.main()
