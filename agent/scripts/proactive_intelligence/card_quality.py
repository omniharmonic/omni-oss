"""Grade understanding-step answers against the synthetic fixtures (spec §14).

Offline: `grade()` is used by tests with the fixtures' own good answers.
On the agent host, by hand (real model, nothing published, nothing sent):

    cd ~/omni_agent/scripts && ~/.hermes/hermes-agent/venv/bin/python -m proactive_intelligence.card_quality \\
        --hermes-path ~/.hermes/hermes-agent --model "$OMNI_PROACTIVE_MODEL" --provider "$OMNI_PROACTIVE_PROVIDER"
"""
import argparse
import json
import re
import sys

from .cards import CardInvalid, grounded_ids, readable_headline, validate_card
from .card_fixtures import ITEMS, packet


_STOP = {'It', 'The', 'A', 'An', 'This', 'That', 'They', 'He', 'She', 'There', 'Open', 'Your', 'You', 'We', 'No', 'Not'}
_TOKEN = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\d[\w:./,-]*|[A-Z][\w\'-]*')


def key_tokens(text):
    """Numbers, dates and times, email-like tokens and capitalised words (not stopwords), lower-cased."""
    return {m.group(0).strip('.,:;').lower() for m in _TOKEN.finditer(text) if m.group(0) not in _STOP}


def source_text(pkt, source):
    """The text a fact citing `source` may rest on: the event or related item content, or the calendar's titles and times."""
    if source == 'calendar':
        cal = pkt.get('calendar') or {}
        return ' '.join(' '.join(str(e.get(k, '')) for k in ('title', 'start', 'end')) for e in cal.get('events', []))
    return ' '.join(str(e.get('content', '')) + ' ' + str(e.get('entity', '')) for key in ('events', 'relatedEvidence')
                    for e in pkt.get(key, []) if e.get('id') == source)


def fact_support(fact, pkt):
    """Share of the fact's key tokens found in its cited source (1.0 when it has none)."""
    tokens = key_tokens(fact['text'])
    if not tokens:
        return 1.0
    have = source_text(pkt, fact['source']).lower()
    return sum(1 for tok in tokens if tok in have) / len(tokens)


def default_recipients(item):
    """Who a draft may address when the item names no one: the event's participants and, for chat, its room."""
    allowed = list(item['event'].get('participants') or [])
    room = re.search(r'^Room: (\S+)', item['event'].get('content', ''), re.M)
    if item['event'].get('channel') == 'message-thread' and room:
        allowed.append(room.group(1))
    return allowed


def known_people(item):
    """The strong keys his notes hold for this item: the exact addresses in the supplied person notes."""
    keys = {a.lower() for r in item.get('related') or [] if r.get('kind') == 'person' for a in re.findall(r'^email: (\S+)$', r.get('content', ''), re.M)}
    return lambda address: isinstance(address, str) and address.strip().lower() in keys


def _as_list(v):
    return v if isinstance(v, list) else [v]


def grade(decision, item):
    expect = item['expect']
    carded = isinstance(decision, dict) and decision.get('action') in ('surface', 'work') and isinstance(decision.get('card'), dict)
    out = {'schema': False, 'headline': False, 'grounded': False, 'groundedRatio': 0.0, 'distinctOptions': False, 'draft': False,
           'factSupport': 0.0, 'supported': False, 'noActionRespected': carded == expect['card']}
    if not expect['card']:
        out.update(schema=True, headline=True, grounded=True, groundedRatio=1.0, distinctOptions=True, draft=True, factSupport=1.0, supported=True)
        return out
    if not carded:
        return out
    raw = decision['card']
    out['headline'] = isinstance(raw.get('headline'), str) and readable_headline(raw['headline'])
    facts = raw.get('context') or []
    try:
        card, dropped = validate_card(raw, item['event'], grounded_ids(packet(item)), known=known_people(item))
    except CardInvalid:
        return out
    out['schema'] = True
    out['groundedRatio'] = (len(facts) - dropped) / len(facts) if facts else 1.0
    out['grounded'] = dropped == 0
    labels = [o['label'].strip().lower() for o in card['options']]
    out['distinctOptions'] = len(set(labels)) == len(labels)
    rec = next((o for o in card['options'] if o['id'] == card['recommended']), None)
    pkt = packet(item)
    supports = [fact_support(f, pkt) for f in card['context']]
    out['factSupport'] = sum(s >= 0.6 for s in supports) / len(supports) if supports else 1.0
    out['supported'] = all(s >= 0.6 for s in supports)
    allowed = item.get('allowedRecipients') or default_recipients(item)
    recipients = [a for o in card['options'] if o.get('draft') for k in ('to', 'cc', 'expectTo', 'roomId') for a in _as_list(o['draft']['payload'].get(k, []))]
    kept_in = all(a in allowed for a in recipients)  # text in a source never widens who gets a message
    wanted = expect.get('draftKind')
    has_draft = wanted is None or bool(rec and rec.get('draft') and rec['draft']['kind'] == wanted) or any(
        o.get('draft', {}).get('kind') == wanted for o in card['options'])
    out['draft'] = kept_in and has_draft
    return out


class FakeReasoner:
    """--dry-run: answers every packet with the fixture's own good answer. Touches nothing."""
    def reason(self, packet):
        item = next(i for i in ITEMS if i['event']['id'] == packet['events'][0]['id'])
        return {'decisions': [item['answer']]}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the understanding step over the synthetic fixtures (manual; the real run is on the agent host only). '
                                     'Read-only: it never publishes cards or writes to the vault or gateway.')
    parser.add_argument('--dry-run', action='store_true', help='use a fake reasoner that returns each fixture\'s good answer (no model, no network)')
    parser.add_argument('--hermes-path')
    parser.add_argument('--hermes-python')
    parser.add_argument('--model')
    parser.add_argument('--provider')
    parser.add_argument('--only', help='run one fixture by name')
    args = parser.parse_args(argv)
    if args.dry_run:
        reasoner = FakeReasoner()
    else:
        if not args.hermes_path or not args.model:
            parser.error('--hermes-path and --model are required unless --dry-run')
        from .hermes_adapter import HermesReasoner
        reasoner = HermesReasoner(args.hermes_path, args.model, args.provider, hermes_python=args.hermes_python)
    checks = ('schema', 'headline', 'grounded', 'supported', 'distinctOptions', 'draft', 'noActionRespected')
    totals, rows, clean = {}, [], 0
    for item in ITEMS:
        if args.only and item['name'] != args.only:
            continue
        try:
            result = reasoner.reason(packet(item))
            decision = (result.get('decisions') or [None])[0]
            g = grade(decision, item)
            card = decision.get('card') if isinstance(decision, dict) else None
            headline = card.get('headline', '') if isinstance(card, dict) else ''
        except Exception as error:
            g, headline = {'error': type(error).__name__}, ''
        rows.append((item['name'], g, headline))
        if all(g.get(k) is True for k in checks):
            clean += 1
        for k in checks:
            totals[k] = totals.get(k, 0) + int(g.get(k) is True)
    for name, g, headline in rows:
        flags = ' '.join('%s=%s' % (k, 'ok' if v is True else v) for k, v in g.items())
        print('%-24s %s | %s' % (name, flags, headline[:90]))
    n = max(1, len(rows))
    print('\nPass rate per check (of %d items):' % len(rows))
    for k in checks:
        print('  %-18s %d/%d' % (k, totals.get(k, 0), len(rows)))
    if args.dry_run:
        print('(--dry-run grades the fixtures against their own answers: it proves wiring, not model quality.)')
    print('Items passing every check: %d/%d (%.0f%%)' % (clean, len(rows), 100.0 * clean / n))
    return 0 if totals.get('schema', 0) / n >= 0.9 else 1


if __name__ == '__main__':
    sys.exit(main())
