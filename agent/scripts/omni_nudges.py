"""M3 local producer: read-only evidence; commit only proposes to the owner gateway.

No tag verdict controls owed-reply eligibility. No message is sent, marked read,
triaged, or relayed. Stdout contains counts only. All timestamps are UTC epoch ms.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: The owner's IANA timezone (identity owner.timezone)

MAX_INVENTORY = 50000


def timestamp(value) -> Optional[int]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value * 1000 if value < 1e11 else value) if value > 0 else None
    if isinstance(value, str):
        if re.fullmatch(r"[0-9]{10,13}", value):
            return timestamp(int(value))
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # Unknown timezone is not reliable inbound evidence (legacy metadata date).
            if dt.tzinfo:
                return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    return None


def channel(note) -> str:
    m = note.metadata or {}
    kind = str(m.get("type") or "")
    path = note.path or ""
    if kind == "email" or path.startswith(("vault/emails/", "vault/email/")):
        return "email"
    if kind == "message-thread" or path.startswith("vault/messages/"):
        return "chat"
    return ""


def gateway_request(path: str, body=None):
    """Owner service token may reach only a plain-HTTP loopback gateway. No redirects."""
    origin = os.environ.get("OMNI_GATEWAY_URL", "http://127.0.0.1:8787").rstrip("/")
    u = urllib.parse.urlsplit(origin)
    if u.scheme != "http" or u.hostname not in {"127.0.0.1", "localhost", "::1"} or u.username or u.password or u.path or u.query or u.fragment:
        raise ValueError("OMNI_GATEWAY_URL must be a loopback origin")
    token = os.environ.get("OMNI_SERVICE_TOKEN", "")
    if len(token) < 16:
        raise ValueError("OMNI_SERVICE_TOKEN missing")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("gateway redirect refused")
    req = urllib.request.Request(origin + "/api/omni" + path, data=None if body is None else json.dumps(body).encode(), headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="GET" if body is None else "POST")
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=20) as response:
        return json.load(response)


def replied_since(sent, thread_id: str, inbound_at: int) -> bool:
    return sent.get(thread_id, 0) >= inbound_at if isinstance(sent, dict) else thread_id in sent


def owed_candidate(note, person, score, sent_threads, now: int, sender: str, inbound_at: int):
    """Tag-independent owed rule; unknown reply evidence never means 'not replied'."""
    from standing_agent.relevance import SELF_ADDRESSES
    m = note.metadata or {}
    if not person or person.is_non_human or sender in SELF_ADDRESSES or any(e.lower() in SELF_ADDRESSES for e in person.emails):
        return None
    if channel(note) == "email":
        tid = str(m.get("threadId") or m.get("thread_id") or "").strip("<>")
        if sent_threads is None or not tid or replied_since(sent_threads, tid, inbound_at):
            return None
    wait = 4 if person.priority_weight >= .7 else 24 if person.priority_weight >= .35 else 48
    if inbound_at > now or now - inbound_at < wait * 3600000 or now - inbound_at > 7 * 86400000:
        return None
    return candidate(note, "reply-owed", score, person, ["Waiting %d h" % ((now-inbound_at)//3600000)])


DEFAULT_AGENT_MATRIX_IDS = ("@omni-agent:localhost",)


def agent_matrix_ids() -> set:
    """The agent's own Matrix senders: its user (MATRIX_USER) and relays of its bots (OMNI_AGENT_MATRIX_IDS)."""
    ids = {i.lower() for i in DEFAULT_AGENT_MATRIX_IDS}
    ids.add(os.getenv("MATRIX_USER", DEFAULT_AGENT_MATRIX_IDS[0]).strip().lower())
    for value in os.getenv("OMNI_AGENT_MATRIX_IDS", "").split(","):
        value = value.strip().lower()
        if re.fullmatch(r"@[^:\s]+:\S+", value):
            ids.add(value)
    return ids


def is_agent_sender(mxid: str) -> bool:
    """Omni / Hermes (or a bridge bot) wrote it: never a message the owner owes a reply to."""
    m = mxid.strip().lower()
    return m in agent_matrix_ids() or "bot:" in m or m.startswith("@_")


_FENCE = re.compile(r"```.*?(?:```|$)|~~~.*?(?:~~~|$)", re.S)


def _inline(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"(\*\*|__|\*|_|~~)(?=\S)(.*?\S)\1", r"\2", text)
    return re.sub(r"\s+", " ", text).strip()


def _body(text: str) -> str:
    text = text or ""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4:]
    return _FENCE.sub(" ", text)


def first_heading(text) -> Optional[str]:
    """The first level-1 markdown heading, as plain words."""
    for line in _body(str(text or "")).split("\n"):
        hit = re.match(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", line)
        if hit and _inline(hit.group(1)):
            return _inline(hit.group(1))
    return None


def humanize_leaf(path: str) -> str:
    """`vault/tasks/app-2_defined.md` -> "App 2 defined"."""
    leaf = re.sub(r"\.(md|markdown|txt)$", "", (path or "").rstrip("/").split("/")[-1], flags=re.I)
    words = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", leaf)).strip()
    return words[:1].upper() + words[1:] if words else "Item"


def plain_text(text, limit: int) -> str:
    """Markdown reduced to plain words, cut at a word boundary with an ellipsis."""
    lines = []
    for line in _body(str(text or "")).split("\n"):
        line = re.sub(r"^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)", "", line)
        if re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line):
            continue
        line = _inline(line)
        if line:
            lines.append(line)
    out = re.sub(r"[\x00-\x1f\x7f]", " ", " ".join(lines)).strip()
    if len(out) <= limit:
        return out
    cut = out[: limit - 1].rsplit(" ", 1)[0] or out[: limit - 1]
    return cut.rstrip(" ,;:-") + "\u2026"


def readable_title(note) -> str:
    """Subject or metadata title, else the first H1 of the preview, else the path leaf in words."""
    m = note.metadata or {}
    for value in (m.get("subject"), m.get("title")):
        if isinstance(value, str) and value.strip():
            return _inline(value)[:200]
    return (first_heading(note.preview) or humanize_leaf(note.path or "Item"))[:200]


SUMMARY_MAX = 280


def plain_summary(text, title: str) -> str:
    """A short plain-text summary; a leading heading that only repeats the title is dropped."""
    body = _body(str(text or ""))
    head = re.match(r"^\s*#{1,6}\s+([^\n]*)(?:\n|$)", body)
    if head and _inline(head.group(1)).lower() == title.lower():
        body = body[head.end():]
    return plain_text(body, SUMMARY_MAX)


def candidate(note, kind, score, person=None, reasons=None):
    m = note.metadata or {}
    priority = float(person.priority_weight) if person else float(score.priority)
    # A natural-language title and a short plain summary — never a slug or a raw markdown preview.
    title = readable_title(note)
    summary = plain_summary(note.preview, title) or title
    return {"sourceId":note.id,"kind":kind,"title":title,"summary":summary,"reasons":(list(reasons or [])+list(score.reasons))[:8],"senderId":None,"baseScore":float(score.score),"priority":priority,"deadline":timestamp(m.get("due") or m.get("deadline")),"urgentAt":timestamp(m.get("lastMessageAt")) if re.search(r"\burgent\b", summary, re.I) else None,"commitment":bool(m.get("commitment_owed"))}


def collect(client, sent_by_account, now: int, previous=None, rows=None, card_ids=frozenset()):
    from standing_agent import relevance, people_resolver
    rows = rows if rows is not None else client.query_notes(limit=MAX_INVENTORY, include_content=False, strict_response=True)
    if len(rows) >= MAX_INVENTORY:
        raise RuntimeError("Inventory cap reached; refusing incomplete sweep")
    context = relevance.build_context(client)
    pending = []
    sender_ids = {}
    resolved = set()
    prior_kinds = {row["sourceId"]:row.get("kind") for row in previous or []}
    prior = {sid for sid,kind in prior_kinds.items() if kind not in {"job","freshness"}}
    by_id = {row.id: row for row in rows}
    for source_id in prior:
        row = by_id.get(source_id)
        if row is None or "prism-trashed" in row.tags or (row.metadata or {}).get("prism_trashed_at"):
            resolved.add(source_id)
    stats = {"scanned":len(rows),"candidates":0,"unknownReplyEvidence":0,"chatReads":0,"chatReadCap":0}
    for row in sorted(rows,key=lambda n:timestamp((n.metadata or {}).get("lastMessageAt")) or 0,reverse=True):
        m=row.metadata or {}; kind=channel(row)
        if prior_kinds.get(row.id)=="unprocessed" and "triaged" in row.tags and "triage-failed" not in row.tags:resolved.add(row.id)
        when=timestamp(m.get("lastMessageAt")) or timestamp(row.updated_at)
        if not kind or when is None or "prism-trashed" in row.tags or m.get("prism_trashed_at"):
            continue
        if now-when>7*86400000:
            # Age closes legacy nudge sources only; a card is closed by answered-elsewhere evidence, never by age.
            if row.id in prior and row.id not in card_ids:resolved.add(row.id)
            continue
        if when>now:continue
        try:score = relevance.score_inbound(row,context)
        except Exception:
            stats["sourceScoreUnavailable"]=stats.get("sourceScoreUnavailable",0)+1;continue
        sender = relevance._sender_email(str(m.get("from") or m.get("sender") or ""))
        try:person = people_resolver.resolve_by_email(sender,client) if sender else None
        except Exception:
            stats["senderLookupUnavailable"]=stats.get("senderLookupUnavailable",0)+1;continue
        inbound_at=when
        if kind == "chat":
            # Display names and body transcript lines are attacker-controlled; never reply evidence.
            mxid=m.get("lastMessageSender");event=m.get("lastMessageEventId")
            if not isinstance(mxid,str) or not mxid.startswith("@") or not isinstance(event,str) or not event:
                stats["chatIdentityUnknown"]=stats.get("chatIdentityUnknown",0)+1
                if prior_kinds.get(row.id)!="reply-owed" and now-when>=2*3600000 and ("triaged" not in row.tags or "triage-failed" in row.tags) and score.score>=.32:
                    fallback=candidate(row,"unprocessed",score,reasons=["Awaiting processing; sender and reply evidence unverified"]);fallback.update(priority=min(fallback["priority"],.59),urgentAt=None);pending.append(fallback)
                continue
            # The agent's own words (its Matrix user, a relay of its bot, a bridge bot) are owner-side:
            # never an owed reply, never "unprocessed" — that would make Omni nudge about itself.
            if m.get("lastMessageIsSelf") is True or m.get("lastMessageIsAgent") is True or is_agent_sender(mxid):
                if row.id in prior:resolved.add(row.id)
                continue
            matches=[]
            for note in rows:
                pm=note.metadata or {}
                if pm.get("type")!="person" and "person" not in note.tags:continue
                ch=pm.get("channels") or {};values=[]
                for value in [pm.get("matrix"),pm.get("matrixId"),pm.get("matrixRoomIds")]+(list(ch.values()) if isinstance(ch,dict) else []):
                    values.extend(value if isinstance(value,list) else [value])
                if mxid.lower() in {v.lower() for v in values if isinstance(v,str) and v.startswith("@")} and "prism-trashed" not in note.tags:matches.append(note)
            if len(matches)!=1:
                stats["chatIdentityUnknown"]=stats.get("chatIdentityUnknown",0)+1
                if prior_kinds.get(row.id)!="reply-owed" and now-when>=2*3600000 and ("triaged" not in row.tags or "triage-failed" in row.tags) and score.score>=.32:
                    fallback=candidate(row,"unprocessed",score,reasons=["Awaiting processing; sender and reply evidence unverified"]);fallback.update(priority=min(fallback["priority"],.59),urgentAt=None);pending.append(fallback)
                continue
            person=people_resolver._make_resolved(matches[0])
            if person.is_non_human:continue
            sender=next(iter(person.emails),"").lower()
            row=replace(row,preview=str(m.get("lastMessageText") or row.preview or "")[:1000],metadata={**m,"from":sender})
            score=relevance.score_inbound(row,context)
        account=str(m.get("account") or "").lower()
        sent=sent_by_account.get(account)
        if kind=="email" and sent is None:stats["unknownReplyEvidence"]+=1
        if kind=="email" and sent is not None and replied_since(sent,str(m.get("threadId") or m.get("thread_id") or "").strip("<>"),inbound_at):
            if row.id in prior:resolved.add(row.id)
            continue
        if sender in relevance.SELF_ADDRESSES or person and any(e.lower() in relevance.SELF_ADDRESSES for e in person.emails):
            if row.id in prior and kind=="email":resolved.add(row.id)
            continue
        owed=owed_candidate(row,person,score,sent,now,sender,inbound_at)
        if owed and (score.score>=.32 or person.priority_weight>=.6):pending.append(owed)
        elif now-when>=2*3600000 and ("triaged" not in row.tags or "triage-failed" in row.tags) and score.score>=.32:
            pending.append(candidate(row,"unprocessed",score,person,["Awaiting processing; reply status unverified"]))
        if pending and pending[-1]["sourceId"] == row.id and person:
            if person.path not in sender_ids:
                try:
                    sender_note = client.get_note(person.path, include_content=False)
                    sender_ids[person.path] = sender_note.id if sender_note else None
                except Exception:
                    stats["senderLookupUnavailable"]=stats.get("senderLookupUnavailable",0)+1
                    sender_ids[person.path]=None
            pending[-1]["senderId"] = sender_ids[person.path]
    stats["candidates"]=len(pending)
    stats["resolvedSourceIds"]=sorted(resolved)
    return pending,stats


class EvidenceUnavailable(RuntimeError):
    """Fixed diagnostic codes only; no command stderr, correspondence or credentials."""
    def __init__(self, code):
        self.code=code
        super().__init__(code)


def unavailable_reason(exc):
    if isinstance(exc,EvidenceUnavailable):return exc.code
    if isinstance(exc,PermissionError):return "file_read_denied"
    if isinstance(exc,subprocess.TimeoutExpired):return "command_timeout"
    if isinstance(exc,FileNotFoundError):return "required_path_missing"
    if isinstance(exc,urllib.error.HTTPError):return "gateway_http_%d"%exc.code
    status=re.search(r"OSStatus (-?\d+)",str(exc))
    if status:return "keychain_osstatus_"+status.group(1)
    text=str(exc)
    if "OMNI_SERVICE_TOKEN missing" in text:return "service_token_missing"
    if "no Bridge password" in text:return "bridge_password_unavailable"
    if "Bridge TLS pin missing" in text:return "bridge_pin_missing"
    return type(exc).__name__


def sent_evidence(accounts, diagnostics=None):
    evidence={}
    for account in sorted(accounts):
        try:
            if account and account==(os.environ.get("PROTON_BRIDGE_USER") or os.environ.get("OMNI_OWNER_EMAIL") or "").strip():
                from proton_mail import sent_thread_ids
                evidence[account]=sent_thread_ids(days=14,strict=True,with_dates=True)
            else:
                since=(datetime.now(timezone.utc)-timedelta(days=14)).strftime("%Y/%m/%d")
                result=subprocess.run(["gog","gmail","messages","search",f"in:sent after:{since}","--max","500","--account",account,"--json"],capture_output=True,text=True,timeout=20)
                if result.returncode:raise RuntimeError("Sent search failed")
                data=json.loads(result.stdout);messages=data.get("messages")
                if not isinstance(messages,list) or len(messages)>=500 or data.get("nextPageToken"):raise RuntimeError("Sent search incomplete")
                dates={};lookups=0;started=time.monotonic()
                for message in messages:
                    if not isinstance(message,dict) or not message.get("threadId"):raise RuntimeError("Sent message malformed")
                    sent_at=timestamp(message.get("internalDate"))
                    if sent_at is None:
                        # gog0.25 search exposes a timezone-less presentation date, not internalDate.
                        # Its metadata get returns the raw Gmail Message under `message`.
                        mid=str(message.get("id") or "")
                        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",mid) or lookups>=100 or time.monotonic()-started>=45:raise RuntimeError("Sent metadata cap reached")
                        lookups+=1
                        detail=subprocess.run(["gog","gmail","get",mid,"--format","metadata","--account",account,"--json"],capture_output=True,text=True,timeout=min(10,max(1,45-(time.monotonic()-started))))
                        if detail.returncode:raise RuntimeError("Sent metadata unavailable")
                        raw=json.loads(detail.stdout).get("message")
                        if not isinstance(raw,dict) or str(raw.get("threadId"))!=str(message["threadId"]) or "SENT" not in (raw.get("labelIds") or []):raise RuntimeError("Sent metadata mismatch")
                        sent_at=timestamp(raw.get("internalDate"))
                    if sent_at is None:raise RuntimeError("Sent timestamp unavailable")
                    tid=str(message["threadId"]);dates[tid]=max(dates.get(tid,0),sent_at)
                evidence[account]=dates
        except Exception as exc:
            evidence[account]=None
            if diagnostics is not None:diagnostics[account]=unavailable_reason(exc)
    return evidence


def calendar_interval(raw):
    from zoneinfo import ZoneInfo
    stamps=[]
    for side in ("start","end"):
        item=raw.get(side) or {}
        stamp=timestamp(item.get("dateTime"))
        if stamp is None and isinstance(item.get("date"),str):
            try:stamp=int(datetime.strptime(item["date"],"%Y-%m-%d").replace(tzinfo=ZoneInfo(item.get("timeZone") or OWNER_TZ)).timestamp()*1000)
            except (ValueError,KeyError):pass
        stamps.append(stamp)
    return tuple(stamps)


def meeting_context(now=None):
    from standing_agent.morning_brief import CALENDAR_ACCOUNT
    now=now or datetime.now(timezone.utc)
    result=subprocess.run(["gog","calendar","events","primary","--from",(now-timedelta(days=1)).isoformat(),"--to",(now+timedelta(days=1)).isoformat(),"--max","1000","--account",CALENDAR_ACCOUNT,"--json"],capture_output=True,text=True,timeout=20)
    if result.returncode:raise EvidenceUnavailable("calendar_command_exit_%d"%result.returncode)
    try:data=json.loads(result.stdout)
    except (ValueError,TypeError):raise EvidenceUnavailable("calendar_json_invalid")
    rows=data.get("events") if isinstance(data,dict) else data
    if not isinstance(rows,list) or len(rows)>=1000 or isinstance(data,dict) and data.get("nextPageToken"):
        raise RuntimeError("Calendar inventory incomplete")
    events=[{"raw":row} for row in rows]
    busy=False
    for event in events:
        raw=event["raw"]
        if raw.get("transparency")=="transparent" or raw.get("status")=="cancelled":continue
        attendees=raw.get("attendees") or []
        if any(a.get("self") and a.get("responseStatus")=="declined" for a in attendees):continue
        if not attendees:continue
        start,end=calendar_interval(raw)
        if start is None or end is None:raise EvidenceUnavailable("calendar_interval_unknown")
        if start<=int(now.timestamp()*1000)<end:busy=True
    return {"inMeeting":busy,"events": [e["raw"] for e in events if e["raw"].get("status")!="cancelled" and e["raw"].get("transparency")!="transparent" and e["raw"].get("attendees") and not any(a.get("self") and a.get("responseStatus")=="declined" for a in e["raw"]["attendees"])]}


def explicit_deadline(value):
    """Only written dates, never natural-language deadline hints; DST aware in the owner's zone."""
    if isinstance(value,str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):
        from zoneinfo import ZoneInfo
        try:return int(datetime.fromisoformat(value).replace(hour=23,minute=59,tzinfo=ZoneInfo(OWNER_TZ)).timestamp()*1000)
        except ValueError:return None
    return timestamp(value)


def structured_candidates(rows, now, previous):
    from types import SimpleNamespace
    from tasks_store import normalize_status, ACTIVE_STATUSES, DONE_STATUSES, SELF_OWNERS
    pending=[];resolved=set();prior={p["sourceId"] for p in previous}
    for row in rows:
        m=row.metadata or {};kind=str(m.get("type") or "")
        if "prism-trashed" in row.tags or m.get("prism_trashed_at"):continue
        task=kind=="task" or "task" in row.tags
        promise=kind=="promise" or "promise" in row.tags
        if not task and not promise:continue
        status=normalize_status(str(m.get("status") or "pending"))
        if status in DONE_STATUSES or promise and str(m.get("status")) in {"kept","broken","withdrawn","fulfilled"}:
            if row.id in prior:resolved.add(row.id)
            continue
        owner=str(m.get("owner") or m.get("assigned") or "").strip().lower()
        if owner not in SELF_OWNERS or promise and m.get("direction") not in {None,"owner_to_other"}:
            if row.id in prior:resolved.add(row.id)
            continue
        if task and status not in ACTIVE_STATUSES:continue
        # A tracked promise's task is its actionable source; don't duplicate it.
        if promise and m.get("task_ref") and m.get("status")=="tracked":
            if row.id in prior:resolved.add(row.id)
            continue
        raw=m.get("deadline") or m.get("due") if task else m.get("stated_deadline")
        deadline=explicit_deadline(raw) if not task or m.get("deadline_source") in {"stated","owner"} else None
        if task and (deadline is None or deadline > now+24*3600000):
            if row.id in prior:resolved.add(row.id)
            continue
        score=SimpleNamespace(score=.45 if promise else .4,priority=.6,reasons=[])
        out=candidate(row,"commitment" if promise else "task",score,reasons=["Written commitment" if promise else "Your active task"])
        out.update(deadline=deadline,commitment=promise,urgentAt=None)
        pending.append(out)
    return pending,resolved


#: A T-15 meeting nudge is only useful if it interrupts. Prism's interruptDecision (nudge-policy.ts)
#: pushes at once only for score >= .85 with a deadline today; anything lower waits for a digest slot
#: (minutes 0-5 of 9/11/13/15/17), which a 15-minute-lived candidate almost never meets. At .7 the
#: gateway score (consequenceScore: base + .15 deadline proximity - feedback penalty) landed exactly on
#: .85 with no margin, so a single noise mark (-.05) sent it to the digest path; 15/15 meeting nudges
#: in 10 days (to 2026-11-11) were resolved without ever surfacing. .9 clears the bar by .2.
MEETING_BASE_SCORE = .9


def meeting_candidates(rows, events, now):
    from types import SimpleNamespace
    out=[]
    for event in events:
        start=timestamp((event.get("start") or {}).get("dateTime"))
        if start is None or not 0<start-now<=15*60000:continue
        matches=[r for r in rows if (r.metadata or {}).get("event_id")==event.get("id") and (timestamp((r.metadata or {}).get("event_start"))==start or datetime.fromtimestamp(start/1000,__import__("zoneinfo").ZoneInfo(OWNER_TZ)).strftime("%Y-%m-%d") in r.path) and "prism-trashed" not in r.tags and not (r.metadata or {}).get("prism_trashed_at")]
        if len(matches)!=1:continue # No invented source ID or ambiguous prep note.
        c=candidate(matches[0],"meeting",SimpleNamespace(score=MEETING_BASE_SCORE,priority=.85,reasons=[]),reasons=["Meeting starts within 15 minutes"])
        c.update(deadline=start,urgentAt=None,commitment=False);out.append(c)
    return out


def operational_plan(rows, jobs, now):
    """Complete lean vault + sanitized Hermes status. Never copy logs or errors."""
    from zoneinfo import ZoneInfo
    local=datetime.fromtimestamp(now/1000,ZoneInfo(OWNER_TZ))
    specs=[("Morning intelligence","agent-insight",28,False),("Sense scans","agent-sense",76,False),("Email sync","email",6,True),("Message sync","message-thread",30,True),("Transcript pipeline","transcript",72,False)]
    plan=[]
    for name,tag,hours,business in specs:
        stamps=[timestamp(r.updated_at) or timestamp(r.created_at) for r in rows if tag in r.tags and "prism-trashed" not in r.tags]
        stamps=[t for t in stamps if t is not None];last=max(stamps) if stamps else None
        if last is not None and now-last<=hours*3600000:continue
        plan.append({"key":"freshness/"+tag+"-"+str(last or 0),"kind":"freshness","title":name+" needs a freshness check","summary":"The source has exceeded its configured freshness window. Check the pipeline before relying on it.","base":.65,"hold":business and (local.weekday()>=5 or not 8<=local.hour<18)})
    for job in jobs:
        jid=str(job.get("id") or "");when=timestamp(job.get("last_run_at"));status=job.get("last_status")
        if not re.fullmatch(r"[a-f0-9]{12}",jid) or status not in {"ok","error",None,"running"}:raise RuntimeError("Job status inventory malformed")
        if status=="error" and (when is None or when>now):raise RuntimeError("Job failure timestamp unavailable")
        if when is None or now-when>7*86400000 or when>now:continue
        if status=="error":
            plan.append({"key":"job/"+jid+"-"+str(when),"kind":"job","title":"Scheduled job needs attention","summary":"The latest run failed. Open Jobs to inspect its status; no automatic retry or outward action has been taken.","base":.7})
    return plan


def operational_candidates(rows, plan, previous, verified_kinds=None):
    """Gateway-only sources. No Parachute create/update, even in committed mode."""
    verified_kinds=verified_kinds if verified_kinds is not None else {"job","freshness"}
    proposals=[];active=set()
    for item in plan:
        kind,key=item["key"].split("/",1)
        source_id="ops_"+kind+"_"+key.replace("-","_")
        active.add(source_id)
        if item.get("hold"):continue
        if kind=="job":
            job_id,run_at=key.rsplit("-",1);proposals.append({"kind":"job","jobId":job_id,"runAt":int(run_at)})
        else:
            subsystem,last_seen=key.rsplit("-",1);proposals.append({"kind":"freshness","subsystem":subsystem,"lastSeen":int(last_seen)})
    resolved={p["sourceId"] for p in previous if p["kind"] in verified_kinds and p["sourceId"].startswith("ops_") and p["sourceId"] not in active}
    return proposals,resolved


def observed_accounts(rows,now):
    return {str((row.metadata or {}).get("account") or "").lower() for row in rows if channel(row)=="email" and (timestamp((row.metadata or {}).get("lastMessageAt")) or timestamp(row.updated_at) or 0)>=now-7*86400000}


def audit_reply_evidence(rows,evidence,now):
    replied=[];unknown_chat=0
    for row in rows:
        m=row.metadata or {};when=timestamp(m.get("lastMessageAt"))
        sent=evidence.get(str(m.get("account") or "").lower());tid=str(m.get("threadId") or "").strip("<>")
        if channel(row)=="chat":
            reply_at=timestamp(m.get("lastSelfReplyAt"));inbound_at=timestamp(m.get("lastReplyInboundAt"))
            if reply_at and inbound_at and now-7*86400000<=inbound_at<=reply_at<=now:replied.append({"sourceId":row.id,"repliedAt":reply_at,"inboundAt":inbound_at})
            elif when and when>=now-7*86400000:unknown_chat+=1
        if channel(row)=="email" and when and when>=now-7*86400000 and isinstance(sent,dict) and sent.get(tid,0)>=when:replied.append({"sourceId":row.id,"repliedAt":sent[tid],"inboundAt":when})
    return replied,unknown_chat


def open_card_sources(state_path=None, now=None, errors=None):
    """sourceKeys of cards the proactive job published (outbox rows published or unknown, not a legacy fallback),
    for events seen in the last 8 days. The sweep treats them like prior nudge sources, so a card whose item was
    answered elsewhere closes even when it has no live nudge. The server ignores keys that are already closed."""
    import sqlite3
    from pathlib import Path
    path=Path(state_path or os.environ.get("OMNI_PROACTIVE_STATE") or Path(__file__).resolve().parents[1]/"state"/"proactive.sqlite3")
    if not path.exists():return []
    now=now if now is not None else int(datetime.now(timezone.utc).timestamp()*1000)
    out={};db=None
    try:
        db=sqlite3.connect("file:"+str(path)+"?mode=ro",uri=True,timeout=10)
        for (payload,) in db.execute("SELECT payload FROM outbox WHERE status IN ('published','unknown')"):
            item=json.loads(payload);event=item.get("event") or {}
            if not (item.get("decision") or {}).get("card") or item.get("fallback")=="legacy":continue
            if not isinstance(event.get("id"),str) or (event.get("occurred_at") or 0)<now-8*86400000:continue
            out[event["id"]]={"sourceId":event["id"],"kind":"card"}
    except Exception as exc:
        # Fail soft: a locked, corrupt or schema-less state DB or a bad row must not abort the nudge sweep.
        if errors is not None:errors.append(unavailable_reason(exc))
        return []
    finally:
        if db is not None:db.close()
    return sorted(out.values(),key=lambda r:r["sourceId"])


def merge_previous(previous,cards):
    known={p["sourceId"] for p in previous}
    return list(previous)+[c for c in cards if c["sourceId"] not in known]


def run(commit=False, context_only=False, weekly_audit=False):
    if context_only and weekly_audit:raise ValueError("Choose context or audit mode")
    if context_only:
        context=meeting_context()
        from parachute_client import ParachuteClient
        client=ParachuteClient(timeout=20)
        rows=client.query_notes(tag="meeting-prep",limit=500,include_content=False,strict_response=True)
        if len(rows)>=500:raise RuntimeError("Meeting prep inventory incomplete")
        pending=meeting_candidates(rows,context["events"],int(datetime.now(timezone.utc).timestamp()*1000))
        if commit:
            previous=gateway_request("/hooks/nudges/sources")
            if previous.get("complete") is not True:raise RuntimeError("Nudge sources incomplete")
            gateway_request("/hooks/nudges/context",{"inMeeting":context["inMeeting"]})
            active={p["sourceId"] for p in pending}
            expired=[p["sourceId"] for p in previous["sources"] if p["kind"]=="meeting" and p["sourceId"] not in active]
            if expired:gateway_request("/hooks/nudges/resolve",{"complete":True,"sourceIds":expired})
            for row in pending:gateway_request("/hooks/nudges",row)
        return {"contextChecked":True,"meetingCandidates":len(pending),"proposed":bool(commit)}
    from parachute_client import ParachuteClient
    from standing_agent.relevance import SELF_ADDRESSES
    client=ParachuteClient(timeout=20)
    previous=gateway_request("/hooks/nudges/sources") if commit else {"sources":[],"complete":True}
    if previous.get("complete") is not True:raise RuntimeError("Nudge source inventory incomplete")
    card_errors=[];card_ids=frozenset()
    if commit:
        cards=open_card_sources(errors=card_errors);card_ids=frozenset(c["sourceId"] for c in cards)
        previous={**previous,"sources":merge_previous(previous["sources"],cards)}
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    rows=client.query_notes(limit=MAX_INVENTORY,include_content=False,strict_response=True)
    if len(rows)>=MAX_INVENTORY:raise RuntimeError("Inventory cap reached")
    accounts=observed_accounts(rows,now)
    sent_failures={}
    evidence=sent_evidence(accounts-{""},sent_failures)
    pending,stats=collect(client,evidence,now,previous=previous["sources"],rows=rows,card_ids=card_ids)
    if card_errors:stats["cardSourcesUnavailable"]=1;stats["cardSourcesUnavailableReason"]=card_errors[0]
    stats["sentUnavailableReasons"]=sorted(set(sent_failures.values()))
    stats["observedEmailAccounts"]=len(accounts)
    stats["unavailableEmailAccounts"]=sum(v is None for v in evidence.values())+int("" in accounts)
    if weekly_audit:
        if "" in accounts or any(v is None for v in evidence.values()):raise RuntimeError("Audit sent evidence incomplete")
        replied,unknown_chat=audit_reply_evidence(rows,evidence,now)
        body={"complete":True,"replies":replied,"unknownChat":unknown_chat,"since":now-7*86400000}
        return gateway_request("/hooks/nudges/audit",body) if commit else {"auditReplies":len(replied),"unknownChat":unknown_chat,"proposed":False}
    extra,terminal=structured_candidates(rows,now,previous["sources"]);pending.extend(extra)
    resolutions=set(stats.pop("resolvedSourceIds"))|terminal
    # Failures of a source-specific fetch preserve that source; other adapters stay usable.
    try:
        calendar=meeting_context();pending.extend(meeting_candidates(rows,calendar["events"],now))
        if commit:gateway_request("/hooks/nudges/context",{"inMeeting":calendar["inMeeting"]})
        active={p["sourceId"] for p in pending if p["kind"]=="meeting"}
        resolutions|={p["sourceId"] for p in previous["sources"] if p["kind"]=="meeting" and p["sourceId"] not in active}
    except Exception as exc:
        stats["calendarUnavailable"]=1;stats["calendarUnavailableReason"]=unavailable_reason(exc)
    jobs=None
    try:
        snapshot=gateway_request("/hooks/nudges/jobs")
        if snapshot.get("complete") is not True or not isinstance(snapshot.get("jobs"),list):raise RuntimeError("Jobs incomplete")
        # Validate the entire sanitized job snapshot before using it for resolutions.
        operational_plan(rows,snapshot["jobs"],now);jobs=snapshot["jobs"]
    except Exception as exc:
        stats["jobsUnavailable"]=1;stats["jobsUnavailableReason"]=unavailable_reason(exc)
    plan=operational_plan(rows,jobs or [],now)
    operations,terminal=operational_candidates(rows,plan,previous["sources"],{"freshness","job"} if jobs is not None else {"freshness"})
    resolutions|=terminal;stats["operationalPlanned"]=len(plan)
    stats["candidates"]=len(pending)+len(operations)
    stats["proposed"]=0
    stats["resolved"]=len(resolutions)
    if commit:
        if resolutions:gateway_request("/hooks/nudges/resolve",{"complete":True,"sourceIds":sorted(resolutions)})
        for row in pending:
            gateway_request("/hooks/nudges",row);stats["proposed"]+=1
        for row in operations:
            gateway_request("/hooks/nudges/operational",row);stats["proposed"]+=1
    return stats
