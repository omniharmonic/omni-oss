#!/usr/bin/env python3
"""Calendar hold engine.

When Omni proposes meeting times, each time becomes a real event on the owner's
calendar: no attendees (so nothing is ever sent), opaque, titled
"HOLD: <Person> -- <topic>", and tagged with the private properties
omni_proposal=<id> and omni_hold=1. The registry in state/scheduling/proposals.json
records which events belong to which proposal.

    place    create one hold per slot (rolls back if any create fails)
    release  delete the holds (optionally keeping one), tolerant of events already gone
    confirm  keep one hold, retitle it, delete the rest
    attach-approval / attach-invite   tie a proposal to the Omni approval of its times draft / its invite draft
    list / get

A hold the owner added guests to is his meeting: no release ever deletes it (the slot is detached with
kept_reason 'has-attendees').

Inviting the other person is NOT done here: that is outbound and goes through
omni_propose.py as a calendar-invite for the owner's approval.
"""
import argparse
import fcntl
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY = Path(os.environ.get("OMNI_HOLDS_REGISTRY") or PROJECT_ROOT / "state" / "scheduling" / "proposals.json")
#: The owner's Google calendar account. Config: OMNI_OWNER_EMAIL. Empty = holds refuse to run.
ACCOUNT = os.environ.get("OMNI_OWNER_EMAIL", "").strip()
CALENDAR = "primary"
DASH = "—"
#: How the owner appears in a confirmed meeting title ("<Person> / <first name> — <topic>").
#: Config: OMNI_OWNER_NAME (its first word); "me" when unset.
OWNER_LABEL = (os.environ.get("OMNI_OWNER_NAME", "").strip().split(" ") or [""])[0] or "me"


class GogError(RuntimeError):
    pass


class LookupInconclusive(GogError):
    """The by-property lookup could not prove which events exist (error or unfetched next page)."""


class PlacingInProgress(ValueError):
    """A 'placing' record is younger than PLACING_MIN_AGE_S: its place() call may still be running."""


PLACING_MIN_AGE_S = 600


class GogNotFound(GogError):
    """The event no longer exists (deleted by hand, or already removed)."""


def _gog_bin() -> str:
    override = os.environ.get("OMNI_GOG_BIN")
    if override:
        return override
    return "/opt/homebrew/bin/gog" if os.path.exists("/opt/homebrew/bin/gog") else (shutil.which("gog") or "gog")


def _gog(args: list) -> dict:
    """The only place that talks to gog. Tests patch this."""
    try:
        r = subprocess.run([_gog_bin(), *args, "--json"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GogError(f"gog failed to run: {e}")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()[:500]
        if "Google API error (404" in msg or "Google API error (410" in msg:
            raise GogNotFound(msg)
        raise GogError(msg or f"gog exited {r.returncode}")
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except json.JSONDecodeError:
        return {}


# ---- registry -------------------------------------------------------------------------------

@contextmanager
def _locked():
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    lock = REGISTRY.parent / (REGISTRY.name + ".lock")
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class RegistryCorrupt(RuntimeError):
    pass


def _load() -> dict:
    """A missing file is an empty registry. Anything unreadable fails closed: never overwritten."""
    try:
        text = REGISTRY.read_text()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise RegistryCorrupt(f"cannot read {REGISTRY}: {e}")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise RegistryCorrupt(f"{REGISTRY} is not valid JSON ({e}); fix or restore {REGISTRY.name}.bak")
    if not isinstance(data, dict):
        raise RegistryCorrupt(f"{REGISTRY} is not a JSON object")
    return data


def _save(data: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    if REGISTRY.exists():  # keep the previous good file
        bak = REGISTRY.with_name(REGISTRY.name + ".bak")
        bfd, btmp = tempfile.mkstemp(dir=str(REGISTRY.parent), prefix=".bak-")  # created 0600
        try:
            with os.fdopen(bfd, "wb") as bf:
                bf.write(REGISTRY.read_bytes())
            os.replace(btmp, bak)
        except BaseException:
            try:
                os.unlink(btmp)
            except OSError:
                pass
            raise
    fd, tmp = tempfile.mkstemp(dir=str(REGISTRY.parent), prefix=".proposals-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, REGISTRY)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hold_title(person: str, topic) -> str:
    return f"HOLD: {person} {DASH} {topic}" if topic else f"HOLD: {person}"


def _confirmed_title(person: str, topic) -> str:
    return f"{person} / {OWNER_LABEL} {DASH} {topic}" if topic else f"{person} / {OWNER_LABEL}"


def _event_id(out: dict):
    if not isinstance(out, dict):
        return None
    ev = out.get("event") if isinstance(out.get("event"), dict) else out
    return ev.get("id")


def _delete_event(account: str, event_id: str) -> None:
    try:
        _gog(["calendar", "delete", CALENDAR, event_id, "--account", account, "--force"])
    except GogNotFound:
        pass


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---- API ------------------------------------------------------------------------------------

def _record(pid, person, email, topic, thread_ref, card_id, slots, account, status, planned=None):
    return {"id": pid, "person": person, "email": email, "topic": topic, "thread_ref": thread_ref,
            "card_id": card_id, "status": status, "created": _now(), "slots": slots, "account": account,
            "planned": planned or []}


def _mutate(pid, fn):
    with _locked():
        data = _load()
        if pid not in data:
            raise KeyError(pid)
        out = fn(data[pid])
        _save(data)
        return out


def _find_events(account, pid, start, end) -> list:
    """Events in [start, end] carrying private omni_proposal == pid, as [{id,start,end}].
    Raises LookupInconclusive on a gog failure or when a next page was not fetched."""
    try:
        out = _gog(["calendar", "events", CALENDAR, "--from", start, "--to", end,
                    "--private-prop-filter", f"omni_proposal={pid}", "--all-pages",
                    "--account", account])
    except GogError as e:
        raise LookupInconclusive(str(e))
    if isinstance(out, dict) and out.get("nextPageToken"):
        raise LookupInconclusive("result has a nextPageToken (not fully fetched)")
    items = out if isinstance(out, list) else (out.get("events") or out.get("items") or [])
    found = []
    for ev in items:
        private = ((ev.get("extendedProperties") or {}).get("private") or {})
        if private.get("omni_proposal") == pid and ev.get("id"):
            found.append({"id": ev["id"],
                          "start": (ev.get("start") or {}).get("dateTime"),
                          "end": (ev.get("end") or {}).get("dateTime"),
                          "attendees": len(ev.get("attendees") or [])})
    return found


def _range(prop):
    wins = prop.get("planned") or prop["slots"]
    return min(w["start"] for w in wins), max(w["end"] for w in wins)


def _wide_range(prop):
    """The lookup window for a release: wide enough to still find a hold he moved to another day."""
    start, end = _range(prop)
    pad = timedelta(days=WIDE_LOOKUP_DAYS)
    return ((_parse(start) - pad).isoformat(), (_parse(end) + pad).isoformat())


WIDE_LOOKUP_DAYS = 120


def _reconcile_locked(prop, found_out=None) -> bool:
    """Add calendar events that carry this proposal's props but are not in the record.
    Returns True only when the lookup was conclusive. `found_out`, when given, receives the events found."""
    if not (prop.get("planned") or prop["slots"]):
        return True
    known = {s["event_id"] for s in prop["slots"]}
    try:
        found = _find_events(prop.get("account", ACCOUNT), prop["id"], *_wide_range(prop))
    except LookupInconclusive:
        return False
    if found_out is not None:
        found_out.extend(found)
    uncovered = [w for w in prop.get("planned", [])
                 if not any(s["start"] == w["start"] and s["end"] == w["end"] for s in prop["slots"])]
    for ev in found:
        if ev["id"] in known:
            continue
        known.add(ev["id"])
        w = uncovered.pop(0) if uncovered else {"start": ev["start"] or "", "end": ev["end"] or ""}
        prop["slots"].append({"start": w["start"], "end": w["end"], "event_id": ev["id"]})
    return True


def _age_s(prop) -> float:
    return (datetime.now(timezone.utc) - _parse(prop["created"])).total_seconds()


def reconcile(proposal_id, *, min_age_s=PLACING_MIN_AGE_S) -> dict:
    """Re-find holds by their private props for a placing / rollback-pending record.
    Returns {"result", "proposal"}; result is one of
      noop              status is neither placing nor rollback-pending
      skipped-young     a 'placing' record younger than min_age_s (its place() may still be running): untouched
      reconciled        lookup conclusive; a 'placing' record is demoted to 'rollback-pending'
      inconclusive      lookup failed or was incomplete; record kept as rollback-pending
    release() then cleans up."""
    state = {}
    def go(prop):
        if prop["status"] not in ("placing", "rollback-pending"):
            state["r"] = "noop"
        elif prop["status"] == "placing" and _age_s(prop) < min_age_s:
            state["r"] = "skipped-young"
        else:
            state["r"] = "reconciled" if _reconcile_locked(prop) else "inconclusive"
            prop["status"] = "rollback-pending"
        return prop
    prop = _mutate(proposal_id, go)
    return {"result": state["r"], "proposal": prop}


def place(person, topic, slots, *, email=None, thread_ref=None, card_id=None, account=ACCOUNT) -> dict:
    if not account:
        raise SystemExit("manage_holds: OMNI_OWNER_EMAIL (or --account) is not set; no hold placed")
    if not person or not slots:
        raise ValueError("person and at least one slot are required")
    for s in slots:
        try:
            ok = _parse(s["end"]) > _parse(s["start"])
        except (KeyError, ValueError, TypeError):
            ok = False
        if not ok:
            raise ValueError(f"invalid slot: {s!r}")
    pid = "prop_" + secrets.token_hex(6)
    title = _hold_title(person, topic)
    planned = [{"start": s["start"], "end": s["end"]} for s in slots]
    # Write-ahead: the proposal exists in the registry before any event does.
    with _locked():
        data = _load()
        data[pid] = _record(pid, person, email, topic, thread_ref, card_id, [], account, "placing", planned)
        _save(data)
    try:
        for s in planned:
            try:
                out = _gog(["calendar", "create", CALENDAR, "--summary", title,
                            "--from", s["start"], "--to", s["end"],
                            "--transparency", "opaque", "--send-updates", "none",
                            "--private-prop", f"omni_proposal={pid}", "--private-prop", "omni_hold=1",
                            "--account", account])
                eid = _event_id(out)
            except GogError as e:
                eid, create_err = None, e
            else:
                create_err = None
            if not eid:
                # The create may have succeeded server-side (timeout, unparseable reply): look it up.
                known = {x["event_id"] for x in get(pid)["slots"]}
                found = [e for e in _find_events(account, pid, *_range({"planned": planned}))
                         if e["id"] not in known]  # inconclusive lookup raises; record stays 'placing'
                if not found:
                    raise create_err or GogError("calendar create returned no event id")
                eid = found[0]["id"]
            _mutate(pid, lambda p, s=s, eid=eid: p["slots"].append(
                {"start": s["start"], "end": s["end"], "event_id": eid}))
        prop = _mutate(pid, lambda p: p.__setitem__("status", "open") or p)
    except Exception:
        _rollback(pid, account)
        raise
    return prop


def _rollback(pid, account):
    """Best effort: delete what was created. Anything left stays recorded so release/reconcile can finish."""
    try:
        with _locked():
            data = _load()
            prop = data.get(pid)
            if prop is None:
                return
            clean = _reconcile_locked(prop)
            stuck = False
            for sl in prop["slots"]:
                if sl.get("deleted"):
                    continue
                try:
                    _delete_event(account, sl["event_id"])
                    sl["deleted"] = True
                except GogError:
                    stuck = True
            if stuck or not clean:
                prop["status"] = "rollback-pending"
            else:
                del data[pid]
            _save(data)
    except Exception:
        pass  # the 'placing' record written ahead of time remains for a later release/reconcile


def _release_locked(data, pid, keep_index, reason):
    prop = data.get(pid)
    if prop is None:
        raise KeyError(pid)
    if prop["status"] in ("released", "confirmed"):
        return prop
    if keep_index is not None:
        if prop["status"] != "open":
            raise ValueError(f"proposal {pid} is {prop['status']}, cannot keep a slot")
        if not isinstance(keep_index, int) or not 0 <= keep_index < len(prop["slots"]):
            raise IndexError(f"keep_index {keep_index!r} out of range for {len(prop['slots'])} slots")
    conclusive = True
    account = prop.get("account", ACCOUNT)
    found = []
    if prop["status"] in ("placing", "rollback-pending"):
        if prop["status"] == "placing" and _age_s(prop) < PLACING_MIN_AGE_S:
            raise PlacingInProgress(f"proposal {pid} is still being placed")
        conclusive = _reconcile_locked(prop, found)  # inconclusive: delete by id, as before
    elif any(i != keep_index and not s.get("deleted") and not s.get("kept_reason") for i, s in enumerate(prop["slots"])):
        # An open proposal: never delete a hold he added guests to (it is a real meeting now). When the calendar
        # cannot say, nothing is deleted: LookupInconclusive propagates and the release is retried.
        found = _find_events(account, pid, *_wide_range(prop))
    guests = {e["id"] for e in found if e.get("attendees")}
    for i, s in enumerate(prop["slots"]):
        if i == keep_index or s.get("deleted") or s.get("kept_reason"):
            continue
        if s["event_id"] in guests:
            s["kept_reason"] = "has-attendees"  # detached: his meeting, never deleted or touched again
            s["detached"] = _now()
            continue
        try:
            _delete_event(account, s["event_id"])
        except GogError:
            _save(data)  # keep progress so a retry only redoes what is left
            raise
        s["deleted"] = True
    if not conclusive:  # an orphan may exist: never forget the record
        prop["status"] = "rollback-pending"
        prop["reason"] = reason + " (lookup inconclusive)"
        _save(data)
        return prop
    prop["status"] = "confirmed" if keep_index is not None else "released"
    prop["reason"] = reason
    prop["closed"] = _now()
    if keep_index is not None:
        prop["confirmed_slot"] = dict(prop["slots"][keep_index])
    _save(data)
    return prop


def release(proposal_id, *, keep_index=None, reason) -> dict:
    with _locked():
        data = _load()
        return _release_locked(data, proposal_id, keep_index, reason)


def confirm(proposal_id, slot_index, *, title=None) -> dict:
    with _locked():
        data = _load()
        prop = data.get(proposal_id)
        if prop is None:
            raise KeyError(proposal_id)
        if prop["status"] == "confirmed" and prop.get("confirmed_slot"):
            return prop
        if prop["status"] not in ("open",):
            raise ValueError(f"proposal {proposal_id} is {prop['status']}, cannot confirm")
        if not 0 <= slot_index < len(prop["slots"]):
            raise IndexError(slot_index)
        kept = prop["slots"][slot_index]
        new_title = title or _confirmed_title(prop["person"], prop.get("topic"))
        try:
            _gog(["calendar", "update", CALENDAR, kept["event_id"], "--summary", new_title,
                  "--private-prop", "omni_hold=0", "--send-updates", "none", "--account", prop.get("account", ACCOUNT)])
        except GogNotFound:
            kept["event_missing"] = True  # The owner removed it by hand: never recreate
        _release_locked(data, proposal_id, slot_index, "confirmed")
        prop = data[proposal_id]
        prop["confirmed_slot"] = dict(kept)
        prop["confirmed_title"] = new_title
        _save(data)
        return prop


def get(proposal_id):
    return _load().get(proposal_id)


def list_proposals(status=None) -> list:
    return [p for p in _load().values() if status is None or p["status"] == status]


def open_proposals() -> list:
    return list_proposals("open")


def attach_card(proposal_id, card_id) -> None:
    with _locked():
        data = _load()
        if proposal_id not in data:
            raise KeyError(proposal_id)
        data[proposal_id]["card_id"] = card_id
        _save(data)


ANNOTATE_KEYS = ("approval_id", "approval_chain", "option_id", "invite_card_id", "invite_state", "confirmed_by", "confirm_pending",
                 "times_sent_at", "followup_card_ids", "invite_approval_id", "invite_approval_chain", "unattached_logged",
                 "send_failed_logged")


def annotate(proposal_id, **fields) -> dict:
    """Set bookkeeping fields the proactive engine keeps on a proposal (never slots or status)."""
    bad = set(fields) - set(ANNOTATE_KEYS)
    if bad:
        raise ValueError(f"cannot annotate {sorted(bad)}")
    return _mutate(proposal_id, lambda p: p.update(fields) or p)


def consume_reply(proposal_id, key, *, outcome, at=None) -> bool:
    """Record that the other person's reply `key` was acted on. False when it already was:
    the same reply is never acted on twice."""
    seen = {}

    def go(p):
        replies = p.setdefault("replies", [])
        seen["new"] = not any(r.get("key") == key for r in replies)
        if seen["new"]:
            replies.append({"key": key, "outcome": outcome, "at": at, "recorded": _now()})
        return p
    _mutate(proposal_id, go)
    return seen["new"]


def retire_confirmed(proposal_id) -> dict:
    """The approved invite created the meeting as its own event: remove the retitled hold so the
    meeting is not on his calendar twice. Only an event that still carries this proposal's private
    property and has no attendees is deleted; one he added guests to by hand is his and stays.
    Raises LookupInconclusive when the calendar cannot say (retry later)."""
    with _locked():
        data = _load()
        prop = data.get(proposal_id)
        if prop is None:
            raise KeyError(proposal_id)
        if prop["status"] != "confirmed" or prop.get("retired"):
            return prop
        kept = prop.get("confirmed_slot") or {}
        if kept.get("event_missing") or not kept.get("event_id"):
            prop["retired"] = "missing"
        else:
            found = _find_events(prop.get("account", ACCOUNT), proposal_id, kept["start"], kept["end"])
            ev = next((e for e in found if e["id"] == kept["event_id"]), None)
            if ev is None:
                prop["retired"] = "missing"
            elif ev.get("attendees"):
                prop["retired"] = "kept-has-attendees"
            else:
                _delete_event(prop.get("account", ACCOUNT), ev["id"])
                prop["retired"] = "deleted"
        prop["retired_at"] = _now()
        _save(data)
        return prop


# ---- CLI ------------------------------------------------------------------------------------

def _thread_ref(raw):
    try:
        ref = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("--thread-ref must be a JSON object")
    if not isinstance(ref, dict) or not ref:
        raise ValueError("--thread-ref must be a non-empty JSON object")
    return ref


def attach_approval(proposal_id, approval_id) -> dict:
    """Tie a proposal to the Omni approval (the draft that offers its times) so the sweep releases the holds when
    that draft is cancelled or expires, and latches 'times sent' when it is sent."""
    if not isinstance(approval_id, str) or not approval_id.strip():
        raise ValueError("approval id is required")
    return annotate(proposal_id, approval_id=approval_id.strip())


def attach_invite(proposal_id, approval_id) -> dict:
    """Tie a CONFIRMED proposal to the Omni approval of its calendar-invite draft (the skill path: confirm, then
    omni_propose calendar-invite, then this). When that approval is sent, the sweep retires the retitled hold so the
    meeting is not on his calendar twice; cancelled or expired leaves the hold as his own entry."""
    if not isinstance(approval_id, str) or not approval_id.strip():
        raise ValueError("approval id is required")
    prop = get(proposal_id)
    if prop is None:
        raise KeyError(proposal_id)
    if prop["status"] != "confirmed":
        raise ValueError(f"proposal {proposal_id} is {prop['status']}: run confirm before attach-invite")
    return annotate(proposal_id, invite_approval_id=approval_id.strip())


def _one_based(n, count, what):
    """The CLI counts the way a person does: 1 = the first slot. The library stays 0-based."""
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= count:
        raise ValueError(f"{what} must be from 1 to {count} (1 = the first slot), got {n!r}")
    return n - 1


def _slot_count(proposal_id) -> int:
    prop = get(proposal_id)
    if prop is None:
        raise KeyError(proposal_id)
    return len(prop["slots"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Calendar holds for proposed meeting times")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("place")
    p.add_argument("--person", required=True)
    p.add_argument("--topic")
    p.add_argument("--email")
    p.add_argument("--slot", action="append", required=True, help="START/END (ISO 8601)")
    p.add_argument("--thread-ref", required=True,
                   help='JSON of the thread the times are offered on, e.g. {"kind":"email","threadId":"...","noteId":"..."}; '
                        'a chat uses {"kind":"message","roomId":"..."}. Without it replies are never noticed.')
    p.add_argument("--card-id")
    p.add_argument("--account", default=ACCOUNT)
    r = sub.add_parser("release")
    r.add_argument("id")
    r.add_argument("--keep", type=int, help="keep this slot: 1 = the first slot")
    r.add_argument("--reason", required=True)
    c = sub.add_parser("confirm")
    c.add_argument("id")
    c.add_argument("n", type=int, help="the slot to keep: 1 = the first slot, up to the number of slots")
    c.add_argument("--title")
    ls = sub.add_parser("list")
    ls.add_argument("--status")
    g = sub.add_parser("get")
    g.add_argument("id")
    aa = sub.add_parser("attach-approval", help="record the omni_propose approval id that offers these times")
    aa.add_argument("id")
    aa.add_argument("approval_id")
    ai = sub.add_parser("attach-invite", help="after confirm: record the omni_propose approval id of the calendar invite")
    ai.add_argument("id")
    ai.add_argument("approval_id")
    rc = sub.add_parser("reconcile")
    rc.add_argument("id")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "place":
            slots = []
            for raw in a.slot:
                start, _, end = raw.partition("/")
                slots.append({"start": start, "end": end})
            out = place(a.person, a.topic, slots, email=a.email,
                        thread_ref=_thread_ref(a.thread_ref),
                        card_id=a.card_id, account=a.account)
        elif a.cmd == "release":
            keep = None if a.keep is None else _one_based(a.keep, _slot_count(a.id), "--keep")
            out = release(a.id, keep_index=keep, reason=a.reason)
        elif a.cmd == "confirm":
            before = get(a.id)
            already = bool(before and before["status"] == "confirmed")
            out = confirm(a.id, _one_based(a.n, _slot_count(a.id), "slot number"), title=a.title)
            # already: the pick was confirmed before (the engine or another turn); its invite may already be in Needs You.
            out = dict(out, already=already)
        elif a.cmd == "attach-approval":
            out = attach_approval(a.id, a.approval_id)
        elif a.cmd == "attach-invite":
            out = attach_invite(a.id, a.approval_id)
        elif a.cmd == "reconcile":
            out = reconcile(a.id)
        elif a.cmd == "list":
            out = list_proposals(a.status)
        else:
            out = get(a.id)
    except RegistryCorrupt as e:
        print(json.dumps({"error": f"registry unreadable, nothing written: {e}"}), file=sys.stderr)
        return 2
    except (GogError, KeyError, ValueError, IndexError) as e:
        print(json.dumps({"error": str(e) or type(e).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
