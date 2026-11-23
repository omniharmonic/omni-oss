"""Daily synthesis modes using the same durable budget and native reasoning stage."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote
from collections import Counter
import os
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: the owner's IANA timezone (identity owner.timezone)

TASKS = {
    'morning': 'Prepare one coherent morning briefing for the owner. From supplied current source evidence, identify today priorities, meetings requiring preparation, explicit deadlines, important incoming correspondence and one useful next step. Distinguish unknown information. The selected calendar evidence is not a complete agenda: do not claim a total meeting count for a day from the included subset. Ground claims with source references only in the evidence array; use readable prose without raw IDs or revision timestamps. Make the brief readable at breakfast; do not emit separate relay cards or claim calendar availability without evidence.',
    'evening': 'Prepare one concise evening review. Summarize observed completed work, unresolved decisions and explicit commitments, then prepare tomorrow priorities from supplied evidence. The selected calendar evidence is not a complete agenda: do not claim a total meeting count for a day from the included subset. Do not infer that a task was completed merely because it is absent. Place source references only in the evidence array and explain uncertainty in readable prose.',
    'tweet': 'Draft one tweet in the voice of the owner from the ideas in the supplied writing, concept and insight notes. Never name people, organisations internal matters, meetings, correspondence, schedule or places: generalise to the underlying idea. No posting is authorized. The event field recent_drafts holds the latest earlier drafts: do not repeat their subject, central claim, opening or key metaphor (a paraphrase is a repeat); pick a different idea from the supplied notes. Put the private rationale in body (which note or notes inspired it) and ONLY the publishable tweet text in draft, aim for 240 characters or fewer and never more than 280 (count them), with no quotes around it. Use action surface whenever a draft exists. Use action ignore only when the supplied notes contain no usable idea, and say why in body.',
    'nightly-review': 'Review recent transcript and knowledge sources for explicit commitments, new decisions and useful connections. Prepare one evidence-linked review with proposed maintenance steps. Do not edit the vault or create facts. Existing nightly cloud graph weaving remains a separate owner until its migration and backup/write gates are approved.'
}


def build(client, name, now, anchor_id=None):
    if name not in TASKS: raise ValueError('unknown routine')
    rows = client.query_notes(limit=50000, include_content=False, strict_response=True)
    if len(rows) >= 50000: raise ValueError('routine inventory incomplete')
    rows = [n for n in rows if n.updated_at and 'prism-trashed' not in (n.tags or []) and not (n.metadata or {}).get('prism_trashed_at')]
    if not anchor_id:
        anchors = [n for n in rows if str((n.metadata or {}).get('type') or '') in ('profile', 'concern', 'mission') or set(n.tags or []) & {'profile', 'concern', 'mission'}]
        anchors.sort(key=lambda n: (str((n.metadata or {}).get('type')) != 'profile', n.id))
        if not anchors: raise ValueError('no existing profile/concern/mission anchor; configure --anchor-id')
        anchor_id = anchors[0].id
    anchor = client.get_note(anchor_id)
    if not anchor or not anchor.updated_at or 'prism-trashed' in (anchor.tags or []): raise ValueError('routine anchor unavailable')
    from .detectors import select, stamp, DAY
    by_id = {n.id: n for n in rows}
    eligible = select(rows, now)
    due = [e for e in eligible if e['active'] and e['kind'] in ('task','commitment') and e.get('deadline') and e['deadline']>=now-7*DAY]
    due.sort(key=lambda e: (e['deadline'], e['id']))
    inbound = [e for e in eligible if e['active'] and e['kind']=='unprocessed']
    inbound.sort(key=lambda e: (e['occurred_at'],e['id']),reverse=True)
    meetings = []
    context = []
    for n in rows:
        m=n.metadata or {}; kind=str(m.get('type') or '')
        status=str(m.get('status') or m.get('event_status') or '').lower()
        if n.id==anchor_id: continue
        if kind=='meeting' and m.get('calendarEventId') and status not in ('cancelled','canceled'):
            begins,ends=stamp(m.get('start')),stamp(m.get('end'))
            if begins and ends and ends>now and begins<=now+7*DAY:
                meetings.append((begins,n.id))
        recent=stamp(n.updated_at)
        context_deadline=stamp(m.get('deadline') or m.get('due') or m.get('stated_deadline'),True)
        current_context=recent and 0<=now-recent<=30*DAY
        expired_context=context_deadline is not None and context_deadline<now-7*DAY
        stable_context=kind in ('concern','mission') and status not in ('done','completed','canceled','cancelled','archived') and current_context and not expired_context
        recent_context=kind in ('report','transcript') and recent and 0<=now-recent<=7*DAY
        if stable_context or recent_context:
            context.append(n)
    if name == 'tweet': meetings, due, inbound, context = [], [], [], []
    meetings.sort()
    context.sort(key=lambda n:(stamp(n.updated_at) or 0,n.id),reverse=True)
    groups = [[sid for _,sid in meetings], [e['id'] for e in due], [e['id'] for e in inbound], [n.id for n in context]]
    limits = (2,3,3,4)
    related_ids=[]
    for group,limit in zip(groups,limits):
        for sid in group[:limit]:
            if sid!=anchor_id and sid not in related_ids: related_ids.append(sid)
    # Fill unused slots with the same relevance order, never arbitrary old reports.
    for group in groups:
        for sid in group:
            if len(related_ids)>=12: break
            if sid!=anchor_id and sid not in related_ids: related_ids.append(sid)
    if name == 'tweet': related_ids = tweet_sources(rows, now)
    meeting_dates={sid:datetime.fromtimestamp(begins/1000,ZoneInfo(OWNER_TZ)).date().isoformat() for begins,sid in meetings}
    available_by_day=Counter(meeting_dates.values())
    requested_meetings=[{'sourceId':sid,'localDate':meeting_dates[sid]} for sid in related_ids if sid in meeting_dates]
    coverage={'scope':'selected upcoming calendar records, not a complete agenda','completeAgenda':False,
        'availableMeetingRecords':len(meetings),'requestedMeetingRecords':len(requested_meetings),
        'availableRecordsByLocalDay':dict(sorted(available_by_day.items())[:12]),'requestedMeetings':requested_meetings}
    # Stable recent sources form bounded targeted reads, not unanchored whole-vault prompting.
    day = datetime.fromtimestamp(now / 1000, ZoneInfo(OWNER_TZ)).date().isoformat()
    boundary = 'routine:' + name + ':' + day
    return dict(id=anchor.id, revision=anchor.updated_at, kind='commitment', entity=anchor.id,
        occurred_at=now, content='Routing anchor only. Its content, age, deadlines and name are not priority evidence; use the independently selected current related sources.',
        url='prism://note/' + quote(anchor.id,safe=''), boundary=boundary, urgent=False,
        related_ids=related_ids, routine=name,calendar_coverage=coverage)


TWEET_TYPES = {'writing', 'essay', 'concept'}
TWEET_NEVER = {'email', 'message-thread', 'meeting', 'transcript', 'task', 'report', 'person'}


def _kinds(n):
    return set(n.tags or []) | {str((n.metadata or {}).get('type') or '')}


def tweet_sources(rows, now, limit=8):
    """Note ids the owner's own thinking offers a tweet: writing/essay/concept notes and agent-insight
    notes from the last 14 days; never correspondence, meetings, tasks or people. Rotated by the owner's local day."""
    from .detectors import stamp, DAY
    ids = []
    for n in rows:
        k = _kinds(n)
        if k & TWEET_NEVER: continue
        recent = stamp(n.updated_at)
        if k & TWEET_TYPES or ('agent-insight' in k and recent and 0 <= now - recent <= 14 * DAY): ids.append(n.id)
    ids = sorted(set(ids))
    if not ids: return []
    day = datetime.fromtimestamp(now / 1000, ZoneInfo(OWNER_TZ)).date().isoformat()
    start = int(day.replace('-', '')) % len(ids)
    return (ids[start:] + ids[:start])[:limit]


PERSON_LIMIT = 20000


def person_names(client):
    """The scrub list, read at publish time (GatewayPublisher), never carried on the routine event:
    an event is model input and run history, and ~1,000 names there both leak and crowd out evidence."""
    rows = client.query_notes(tag='person', limit=PERSON_LIMIT, include_content=False, strict_response=True)
    if len(rows) >= PERSON_LIMIT: raise ValueError('person inventory incomplete')
    return private_names(rows)


def private_names(rows):
    """Display names of person notes, which a tweet must never mention."""
    names = set()
    for n in rows:
        if 'person' not in (n.tags or []): continue
        name = str((n.metadata or {}).get('name') or str(n.path or '').rsplit('/', 1)[-1]).strip()
        if name and name != 'The owner': names.add(name)
    return sorted(names)


_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
_PHONE = re.compile(r'(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)')

_DATEISH = re.compile(r'\d{4}-\d{2}-\d{2}|\d{4}\s*-\s*\d{4}')


def tweet_privacy_hits(draft, private_names):
    """Reasons a draft is held back: an email address, a phone number, or a person's name."""
    hits = []
    if _EMAIL.search(draft): hits.append('email address')
    if any(sum(c.isdigit() for c in m.group()) >= 10 and not _DATEISH.fullmatch(m.group().strip()) for m in _PHONE.finditer(draft)): hits.append('phone number')
    for name in private_names:
        # Single-word names (Will, Grace) are too ambiguous to scrub; the prompt guards them.
        if name and len(name.split()) > 1 and re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', draft, re.I): hits.append('name: ' + name)
    return hits


TWEET_MAX = 280
#: Earlier drafts handed to the tweet run as negative examples (State.recent_drafts).
RECENT_DRAFTS = 14
_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def tweet_parts(decision):
    """(draft or None, rationale). The rationale is the decision body; the publishable tweet is its
    draft, kept only when it is 1-280 characters after trimming."""
    rationale = (decision.get('body') or '').strip()
    draft = decision.get('draft')
    draft = draft.strip() if isinstance(draft, str) else ''
    return (draft if 1 <= len(draft) <= TWEET_MAX else None), rationale


def routine_day(event):
    """The run's local day from its boundary 'routine:<name>:<YYYY-MM-DD>'."""
    day = str(event.get('boundary') or '').rsplit(':', 1)[-1]
    if not _DAY.match(day):
        raise ValueError('routine event has no day boundary')
    return day
