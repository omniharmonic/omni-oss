"""Read-only deterministic evidence collection. No model calls or vault writes.

Reconciliation reads the light index, then only full bodies for eligible items.
All public timestamps are epoch milliseconds and source revisions remain exact.
"""
import hashlib
import json
import re
import time
from datetime import datetime, time as day_time
from zoneinfo import ZoneInfo
from urllib.parse import quote
import os
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: the owner's IANA timezone (identity owner.timezone)

LIMIT = 50000
BODY_LIMIT = 4000
DAY = 86400000
ZONE = ZoneInfo(OWNER_TZ)


def stamp(value, date_only=False):
    if isinstance(value, bool): return None
    if isinstance(value, (int, float)): return int(value if value > 1e11 else value * 1000)
    if not isinstance(value, str): return None
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            if not date_only: return None
            dt = datetime.combine(datetime.fromisoformat(value).date(), day_time(23, 59), ZONE)
        else:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if dt.tzinfo is None: return None
        return int(dt.timestamp() * 1000)
    except (ValueError, OverflowError): return None


def metadata(note):
    return note.metadata if isinstance(note.metadata, dict) else {}


ADDRESS = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


def participants(m):
    """Strong keys of the people on an item: exact email addresses and a Matrix sender id."""
    found = set()
    for field in ('from', 'sender', 'to', 'cc', 'attendees', 'participants'):
        values = m.get(field) or []
        for value in values if isinstance(values, list) else [values]:
            if isinstance(value, dict):
                value = value.get('email') or value.get('address') or ''
            if isinstance(value, str):
                found.update(a.lower() for a in ADDRESS.findall(value))
    sender = m.get('lastMessageSender')
    if isinstance(sender, str) and sender.startswith('@') and ':' in sender:
        found.add(sender.strip())
    return sorted(found)[:12]


def room_of(m):
    """The chat room an item lives in (a Matrix room id), or '' when it has none."""
    room = m.get('matrixRoomId') or m.get('roomId')
    return room.strip() if isinstance(room, str) and room.strip().startswith('!') and ':' in room else ''


SCHEDULING_WORD = re.compile(r'\b(?:schedul(?:e|ed|ing)|availability|available|free|meet(?:ing)?|chat|call|join|times?|options?)\b', re.I)
SCHEDULING_REQUEST = re.compile(r"\b(?:can|could|would|when|what|which|find|share|work(?:s|ing)?|prefer(?:red)?|let(?:'s| us)|how about)\b", re.I)
DATE_OR_TIME = re.compile(r'\b(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{4}-\d{2}-\d{2})\b', re.I)


def scheduling_cue(m):
    """Metadata-only admission hint; the model still decides whether the owner needs to act."""
    text = m.get('lastMessageText')
    if not isinstance(text, str) or not text.strip():
        return False
    scheduling = SCHEDULING_WORD.search(text)
    request = SCHEDULING_REQUEST.search(text)
    timed = DATE_OR_TIME.findall(text)
    return bool((scheduling and request) or (request and timed) or len(timed) >= 2)


def event(note, kind, now, active=True, boundary='', deadline=None, reason=''):
    m = metadata(note)
    title = str(m.get('title') or m.get('subject') or 'Untitled item')[:200]
    safe_meta = {k: m[k] for k in ('title','subject','from','sender','project','status','deadline','deadline_source','due','start','end','attendees','location','meetLink','event_status','matrixRoomId') if k in m}
    content = f'Title: {title}\nReason to review: {reason}\nFields: {json.dumps(safe_meta, ensure_ascii=False)[:2500]}'
    return dict(id=note.id, revision=note.updated_at, kind=kind, entity=note.id,
                occurred_at=stamp(note.updated_at) or now, content=content,
                url='prism://note/' + quote(note.id, safe=''), active=active,
                channel=str(m.get('type') or '').lower(), mail_account=str(m.get('account') or '').lower(),
                mail_thread_id=str(m.get('threadId') or m.get('thread_id') or '').strip('<>'),
                inbound_at=stamp(m.get('lastMessageAt')) or stamp(m.get('date')) or stamp(note.updated_at),
                boundary=boundary, deadline=deadline, participants=participants(m), room=room_of(m),
                urgent=bool(deadline and now <= deadline <= now + 3600000))


def select(rows, now, score=None, watchers=None, deadline_baselines=None):
    """Pure eligibility rules. Inactive records explicitly retire cached work."""
    from tasks_store import normalize_status, ACTIVE_STATUSES, SELF_OWNERS
    from standing_agent.relevance import SELF_ADDRESSES, _sender_email
    selected = {}
    calendars = []
    by_id = {r.id: r for r in rows}
    for n in rows:
        m = metadata(n); tags = set(n.tags or []); kind = str(m.get('type') or '').lower()
        if not n.updated_at: continue
        relevant = kind in {'email','message-thread','task','promise','commitment','meeting','transcript','trip'} or bool(tags & {'task','transcript','meeting','trip'})
        if not relevant: continue
        inactive = 'prism-trashed' in tags or bool(m.get('prism_trashed_at'))
        ev = event(n, 'commitment', now, active=False)
        if not inactive and (kind in {'email','message-thread'}):
            when = stamp(m.get('lastMessageAt')) or stamp(n.updated_at)
            sender = _sender_email(str(m.get('from') or m.get('sender') or ''))
            self_message = sender in SELF_ADDRESSES or m.get('lastMessageIsSelf') is True
            closed = str(m.get('status') or '').lower() in {'done','resolved','replied','archived'} or bool(tags & {'replied','archived'})
            important = bool(tags & {'important','action-required','urgent','priority'})
            scheduling = kind == 'message-thread' and scheduling_cue(m)
            if score is not None:
                important = important or score(n) >= .5
            if when and 0 <= now-when <= 7*DAY and not self_message and not closed and (important or scheduling):
                reason = ('Chat may contain a scheduling invitation or proposed times. Check the complete calendar, notify the owner, and propose grounded response options.'
                          if scheduling else 'New or changed important correspondence. Verify whether a reply is needed; do not assume reply debt.')
                ev = event(n,'unprocessed',now,reason=reason)
        elif not inactive and (kind == 'task' or 'task' in tags or kind in {'promise','commitment'}):
            status = normalize_status(str(m.get('status') or 'pending'))
            owner = str(m.get('owner') or m.get('assigned') or '').strip().lower()
            deadline = stamp(m.get('deadline') or m.get('due') or m.get('stated_deadline'), True)
            verified = m.get('deadline_source') in {'stated','owner'} or (kind in {'promise','commitment'} and bool(m.get('stated_deadline')))
            if status in ACTIVE_STATUSES and owner in SELF_OWNERS and deadline and verified and deadline <= now + 7*DAY:
                # One stable boundary per urgency band; no daily repeated overdue nags.
                band = 'overdue' if deadline < now else 'hour' if deadline <= now+3600000 else 'day' if deadline <= now+DAY else 'week'
                ev=event(n,'task' if kind=='task' or 'task' in tags else 'commitment',now,boundary=band,deadline=deadline,reason='Explicit deadline: '+band)
        elif not inactive and (kind == 'meeting' or 'meeting' in tags) and m.get('calendarEventId'):
            start, end = stamp(m.get('start')), stamp(m.get('end'))
            if m.get('event_status') not in {'cancelled','canceled'} and start and end and end > now and start <= now+7*DAY:
                calendars.append((n,start,end))
                if start > now and start <= now+DAY:
                    ev=event(n,'meeting',now,boundary='hour' if start<=now+3600000 else 'day',deadline=start,reason='Prepare a useful meeting brief from supplied context; distinguish missing information.')
            title = str(m.get('title') or '')
            travel_hint = bool(re.search(r'\b(trip|travel|flight|airport|conference|retreat)\b', title, re.I))
            if m.get('event_status') not in {'cancelled','canceled'} and start and now < start < now+90*DAY and travel_hint and not ev['active']:
                ev=event(n,'travel',now,reason='Calendar suggests possible travel. Verify location, remote attendance and existing bookings before offering logistics research; do not assume a flight is needed.')
        elif not inactive and (kind=='transcript' or 'transcript' in tags):
            when=stamp(n.updated_at)
            if when and 0<=now-when<=DAY and not (tags & {'processed','commitments-extracted'}):
                ev=event(n,'transcript',now,reason='Review recent transcript for explicit commitments; propose, do not invent or silently write task pages.')
        elif not inactive and (kind=='trip' or 'trip' in tags):
            start=stamp(m.get('start') or m.get('date'),True)
            if start and now<start<now+90*DAY:
                ev=event(n,'travel',now,reason='Upcoming explicit trip. Offer planning help and identify missing details; do not book or purchase.')
        ev['resolved'] = bool(inactive or str(m.get('event_status') or '').lower() in {'cancelled','canceled'} or str(m.get('status') or '').lower() in {'done','completed','resolved','replied','cancelled','canceled','archived'} or tags & {'replied'})
        selected[n.id]=ev
    # Timed overlaps: both real event revisions accompany the model packet.
    calendars.sort(key=lambda r:r[1])
    for i,(a,start,end) in enumerate(calendars):
        for b,bs,be in calendars[i+1:]:
            if bs>=end: break
            for n in (a,b):
                ev=selected[n.id]
                if not ev['active']: ev=event(n,'meeting',now,boundary='conflict',reason='Calendar time overlap; verify intent before suggesting a change.')
                ev['content']+='\nPotential overlap with source '+(b.id if n.id==a.id else a.id)
                selected[n.id]=ev
    for watcher in watchers or []:
        n=by_id.get(watcher['source_id'])
        if not n or not n.updated_at: continue
        m=metadata(n)
        if 'prism-trashed' in (n.tags or []) or m.get('prism_trashed_at'):continue
        changed=n.updated_at!=watcher['revision']
        if watcher['condition']=='deadline_changed':
            current=json.dumps([m.get('deadline'),m.get('due'),m.get('stated_deadline')],sort_keys=True)
            baseline=(deadline_baselines or {}).get(n.id,watcher.get('deadline_baseline'))
            changed=baseline is not None and baseline!=current
        if changed:
            selected[n.id]=event(n,'commitment',now,reason='An owner-requested watch condition changed.')
    paths = {n.path:n.id for n in rows if n.path}
    for ev in selected.values():
        m=metadata(by_id[ev['id']]); ids=[]
        for field in ('project','projects','people','person','source_ref','task_ref','meetingNoteId','transcriptNoteId'):
            values=m.get(field,[]); values=values if isinstance(values,list) else [values]
            for v in values:
                if not isinstance(v,str): continue
                v=v.removeprefix('[[').removesuffix(']]').split('|')[0]
                sid=v if v in by_id else paths.get(v)
                if sid and sid!=ev['id'] and sid not in ids: ids.append(sid)
        ev['related_ids']=ids[:4]
    return list(selected.values())


def collect(client=None, now=None, management=None, source_health_guard=None, on_rows=None):
    """Return a single complete index batch or unavailable; bodies fail partial. `on_rows` receives the complete
    index this pass read (scheduling.VaultInbound reads proposal replies from it: the same data, no second read)."""
    from parachute_client import ParachuteClient
    from standing_agent.relevance import build_context, score_inbound
    client=client or ParachuteClient()
    now=now or int(time.time()*1000)
    try:
        rows=client.query_notes(limit=LIMIT,include_content=False,strict_response=True)
        if len(rows)>=LIMIT: raise ValueError('inventory cap')
        if on_rows is not None: on_rows(rows)
        ctx=build_context(client)
        if management and hasattr(management,'refresh_pending_watchers'):
            management.refresh_pending_watchers(rows)
        watchers=management.active_watchers() if management else []
        baselines={}
        if management:
            management.db.execute('CREATE TABLE IF NOT EXISTS detector_deadlines(id TEXT PRIMARY KEY,value TEXT)')
            baselines=dict(management.db.execute('SELECT id,value FROM detector_deadlines'))
        events=select(rows,now,lambda n: score_inbound(n,ctx).score,watchers,baselines)
        if source_health_guard is not None:
            blocked={e['id'] for e in source_health_guard.filter(events)['blocked']}
            events=[dict(e,queued=False,ingestion_health='unverified') if e['id'] in blocked else e for e in events]
        health='healthy'
        # Cache by exact source revision; bound new full-body reads per pass.
        cache = {}
        if management:
            management.db.execute('CREATE TABLE IF NOT EXISTS detector_bodies(id TEXT PRIMARY KEY,revision TEXT,body TEXT)')
            cache = {r[0]:(r[1],r[2]) for r in management.db.execute('SELECT id,revision,body FROM detector_bodies')}
        active=[e for e in events if e['active'] and e.get('queued') is not False]
        active.sort(key=lambda e:(not e['urgent'], e['deadline'] or float('inf'),-e['occurred_at']))
        reads=0
        deferred=set()
        for e in active:
            try:
                cached=cache.get(e['id'])
                if cached and cached[0]==e['revision']:
                    body=cached[1]
                else:
                    if reads>=40:
                        deferred.add(e['id']); continue
                    reads+=1
                    n=client.get_note(e['id'])
                    if not n or n.updated_at!=e['revision']:raise ValueError('source changed')
                    body=str(n.content or '')[:BODY_LIMIT]
                    if management:
                        with management.db:
                            management.db.execute('INSERT OR REPLACE INTO detector_bodies VALUES(?,?,?)',(e['id'],e['revision'],body))
                e['content']+='\nSource text (untrusted):\n'+body
            except Exception:
                health='partial'
                break
        events=[e for e in events if e['id'] not in deferred]
        deadline_updates={}
        if management and health=='healthy':
            included={e['id'] for e in events}
            for w in watchers:
                n=next((n for n in rows if n.id==w['source_id']),None)
                if n and (n.id in included or n.id not in baselines):
                    m=metadata(n)
                    deadline_updates[n.id]=json.dumps([m.get('deadline'),m.get('due'),m.get('stated_deadline')],sort_keys=True)
        cursor=hashlib.sha256(json.dumps(sorted((n.id,n.updated_at) for n in rows)).encode()).hexdigest()
        return [dict(source='vault',cursor=cursor,events=events,health=health,deadline_updates=deadline_updates)]
    except Exception:
        return [dict(source='vault',cursor=None,events=[],health='unavailable')]
