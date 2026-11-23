"""The conductor: one loop in the gateway that joins, watches, answers and leaves.

Every few seconds it reads the grants (store.py) and the bot's status, and:

* joins an approved meeting two minutes before it starts — listening only (Chrome's silent fake
  microphone) unless speaking was approved, in which case with Omni's voice path;
* rejoins with or without a voice when speaking is approved or revoked mid-meeting;
* when the owner's settled caption names Omni and speaking is approved, asks a full Omni turn
  (assistant.py) what to answer, passes the answer through the speech checks (and, with others in the
  call, a tool-less privacy review), and only then speaks it — and sends the owner every line it said,
  and every line it held back;
* leaves at the scheduled end (plus a grace period), when the bot exits, or when Omni has been alone in
  the call for two minutes, and tells the owner;
* has the Minutes app record every meeting it is in (minutes.py; ``OMNI_MEET_MINUTES=off`` turns every
  Minutes call off and keeps Chrome muted): Chrome plays the call only when it is safe to (see
  ``recording_plan``), a recording starts when Omni is admitted, is checked every 30 s and restarted as a
  new segment if it stopped, and is stopped on every way out;
* after a recorded meeting, names its speakers (the ``naming`` stage, see "Naming speakers" below and
  speakers.py; ``OMNI_MEET_SPEAKERS=off`` turns it off): confident ones automatically, the rest on one
  "Confirm speakers" review in the Omni app, whose answers it applies to Minutes;
* keeps ``~/.hermes/omni-meet/busy.json`` while a meeting is on or Minutes is processing one of ours, so
  the memory guard does not evict models mid-call.

Nothing here can create a grant. Only one meeting at a time (the bot's own limit).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import assistant, gcal, hooks, meetbot, minutes, rules, speakers, speech, store

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[3]
TICK_SECONDS = 3.0


# ── side effects, each replaceable in tests ─────────────────────────────────

def notify(text: str) -> None:
    """The owner's own Telegram chat (send_telegram.py is pinned to it). Never anyone else."""
    try:
        subprocess.run([str(REPO / "venv/bin/python"), str(REPO / "scripts/send_telegram.py")], input=text,
                       text=True, timeout=60, capture_output=True, cwd=str(REPO / "scripts"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("omni-meet: could not notify the owner: %s", type(exc).__name__)


def ask_model(prompt: str, system: str = rules.SYSTEM, schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A tool-less model call (omni_llm, out of process). Used for the privacy review of what Omni is about
    to say when others are in the call."""
    req = json.dumps({"prompt": prompt, "system": system, "schema": schema or rules.SCHEMA, "timeout": 45})
    env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME", "LANG", "PATH", "TMPDIR", "TZ") if k in os.environ}
    try:
        out = subprocess.run([str(REPO / "venv/bin/python"), str(Path(__file__).with_name("reply_worker.py"))],
                             input=req, text=True, capture_output=True, timeout=90, env=env)
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001 — any failure is silence
        return {"speak": False, "text": "", "reason": "model call failed: %s" % type(exc).__name__}


def say(meeting_dir: str, text: str) -> float:
    return speech.say(Path(meeting_dir), text)


def ask_omni(prompt: str, session: str) -> Optional[str]:
    return assistant.ask_omni(prompt, session)


#: The bot's caption keeper writes the participant count here every few seconds (bot_launcher).
PRESENCE_FILE = "omni-presence.json"
PRESENCE_FRESH_S = 60


def presence(meeting_dir: Optional[str], now: Optional[float] = None) -> Optional[int]:
    """People in the call (Omni included), or None when unknown or stale."""
    if not meeting_dir:
        return None
    try:
        data = json.loads((Path(meeting_dir) / PRESENCE_FILE).read_text())
        count, at = data.get("count"), float(data.get("at", 0))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None
    if (now if now is not None else time.time()) - at > PRESENCE_FRESH_S:
        return None
    return count


def is_solo(meeting_dir: Optional[str]) -> bool:
    """Only the owner and Omni: exactly two people, known and fresh. Unknown counts as NOT solo."""
    return presence(meeting_dir) == 2


def alone(meeting_dir: Optional[str]) -> bool:
    """Only Omni is left in the call (known and fresh). Unknown counts as NOT alone."""
    return presence(meeting_dir) == 1


#: Omni leaves once it has been the only one in the call this long.
ALONE_LEAVE_SECONDS = 120

# ── recording (Minutes) ─────────────────────────────────────────────────────

MINUTES_CHECK_SECONDS = 30
#: Start attempts per join (the first, its retry, and restarts after Minutes stopped), and the waits
#: between consecutive failed attempts. Bounded: no tight loops, no endless notifications.
MINUTES_MAX_STARTS = 4
MINUTES_RETRY_DELAYS = (10, 30, 60)
#: Minutes refuses a new start (``busy``) until the previous recording has been processed. That wait is
#: not a failure: Omni checks again this often, without spending a start attempt.
MINUTES_PROCESSING_WAIT_SECONDS = 15
#: A start that timed out may still have begun; a recording this young that is nobody's is adopted.
MINUTES_ADOPT_MAX_AGE_SECONDS = 60
#: Stop treating a stopped recording as "processing" after this long, whatever Minutes says.
MINUTES_PROCESSING_MAX_SECONDS = 3 * 3600

ECHO_REASON = ("the agent host's sound output is still %s, so recording would echo into the call. "
               "Set the default output to BlackHole 16ch to fix" % speech.VOICE_DEVICE)


def recording_plan(enabled: bool, st: Optional[Dict[str, Any]], err: Optional[minutes.MinutesError], speaking: bool,
                   voice_device: str = speech.VOICE_DEVICE) -> Tuple[bool, Optional[str]]:
    """Whether Chrome may play the call (so Minutes can record it), and if not, why — in words for the owner.

    Unmute only when Minutes is on, reachable and ready, AND either Omni will not speak, or the Mac's
    default output is not Omni's own microphone (``voice_device``): with speaking on, call audio played
    into that device would loop back into the call. An unknown output device counts as unsafe.
    ``(False, None)`` = Minutes is switched off (nothing to tell)."""
    if not enabled:
        return False, None
    if err is not None:
        return False, minutes.describe(err)
    st = st or {}
    if st.get("ready") is False:
        return False, "Minutes is not ready (%s)" % (st.get("not_ready_reason") or "no reason given")
    if speaking:
        out = str(st.get("default_output_device") or "").strip()
        if not out or out.lower() == voice_device.lower():
            return False, ECHO_REASON if out else ("Omni could not tell which sound output the agent host uses, so "
                                                   "recording could echo into the call")
    return True, None


def _attendees(m: Dict[str, Any]) -> List[Dict[str, str]]:
    out = []
    names = m.get("attendee_names") if isinstance(m.get("attendee_names"), dict) else {}
    for a in m.get("attendees") or []:
        if isinstance(a, str) and a and a != rules.AGENT_EMAIL:
            out.append(dict({"email": a}, **({"name": str(names[a])} if names.get(a) else {})))
        elif isinstance(a, dict) and (a.get("email") or a.get("name")) and a.get("email") != rules.AGENT_EMAIL:
            out.append({k: str(a[k]) for k in ("name", "email") if a.get(k)})
    return out


def local_seconds(ts: Optional[float] = None) -> int:
    d = datetime.fromtimestamp(ts if ts is not None else time.time())
    return d.hour * 3600 + d.minute * 60 + d.second


def _title(m: Dict[str, Any]) -> str:
    return m.get("title") or m.get("code") or "the meeting"


def _when(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%a %-I:%M %p")
    except (TypeError, ValueError):
        return "?"


# ── naming speakers (speaker-review contract; speakers.py holds the pure parts) ──

SPEAKERS_ENV = "OMNI_MEET_SPEAKERS"
#: Minutes ``get`` / Prism review polls at most this often per meeting …
NAMING_POLL_SECONDS = 30
#: … and this often once the review is closed (only owner corrections can still arrive).
NAMING_IDLE_POLL_SECONDS = 300
#: A recording stopped longer ago than this (e.g. before this feature shipped) is never named.
NAMING_START_WINDOW_SECONDS = 24 * 3600
#: Minutes unreachable / the recording gone / Prism unreachable this long: give up, tell the owner once.
NAMING_GIVE_UP_SECONDS = 3600
#: Every ask row answered, or this long after posting: export, then close the review.
REVIEW_ANSWER_SECONDS = 24 * 3600
#: Answers (corrections included) are applied this long after posting.
REVIEW_CORRECTION_SECONDS = 7 * 86400
NAMING_CLIP_SECONDS = 10
#: Clips in one review POST, in total (each is also capped by minutes.CLIP_MAX_BYTES).
REVIEW_CLIP_BUDGET_BYTES = 8 * 1024 * 1024
#: Minutes errors that mean "try again on a later tick".
NAMING_RETRY_CODES = ("busy", "unavailable", "timeout")
NAMING_LIVE_STAGES = ("waiting", "planned", "review")
#: Per-row working data never written to the store (the cluster's words stay with Minutes).
_NAMING_PRIVATE_FIELDS = ("texts", "evidence")


def speakers_enabled(env=None) -> bool:
    """On unless ``OMNI_MEET_SPEAKERS`` is off/0/false/no — and never when Minutes itself is switched off."""
    e = os.environ if env is None else env
    return minutes.enabled(e) and e.get(SPEAKERS_ENV, "").strip().lower() not in ("off", "0", "false", "no")


def _naming_new_mids(m: Dict[str, Any]) -> List[str]:
    n = m.get("naming") or {}
    done = set(n.get("mids") or []) | set(n.get("done_mids") or [])
    return [str(i) for i in (m.get("minutes_meeting_ids") or []) if i and str(i) not in done]


def naming_due(m: Dict[str, Any], now: float) -> bool:
    """Omni has left a meeting it recorded, the recording has stopped (recently), and some of its Minutes
    recordings have not been named yet."""
    if m.get("status") not in ("done", "cancelled", "failed") or m.get("recording"):
        return False
    if (m.get("naming") or {}).get("stage") in NAMING_LIVE_STAGES:
        return False
    stopped = m.get("recording_stopped_at")
    if not stopped or now - float(stopped) > NAMING_START_WINDOW_SECONDS:
        return False
    return bool(_naming_new_mids(m))


class _Retry(Exception):
    """A Minutes call to repeat on a later tick (busy / unavailable / timeout)."""


# ── the loop ────────────────────────────────────────────────────────────────

class Conductor:
    def __init__(self, own_names: Optional[List[str]] = None):
        extra = [n.strip() for n in os.environ.get("OMNI_MEET_SELF_NAMES", "").split(",") if n.strip()]
        self.own_names = [rules.GUEST_NAME] + extra + list(own_names or [])
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # Public for tests: one pass, at time `now`.
    def tick(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        try:
            self._step(now)
        finally:
            self._update_busy(now)
            try:
                self._naming(now)
            except Exception:  # noqa: BLE001 — naming never stops the meeting loop
                logger.exception("omni-meet: speaker naming failed")

    def _step(self, now: float) -> None:
        meetings = store.load()
        status = meetbot.status()
        live = [k for k, m in meetings.items() if m.get("status") in ("joining", "joined")]
        for key in live:
            self._tend(key, meetings[key], status, now)
        for key, m in meetings.items():   # out of the call by any other way (e.g. attendance cancelled by a tool)
            if key not in live and m.get("recording") and not (m.get("rejoining") and m.get("status") == "scheduled"):
                self._minutes_stop(key, now)
        if any(store.load().get(k, {}).get("status") in ("joining", "joined") for k in live):
            return
        due = sorted((m for m in store.load().values() if m.get("status") == "scheduled" and rules.should_be_in(m, now)),
                     key=lambda m: float(m.get("start") or 0))
        if due:
            self._join(due[0], status, now)
        for m in store.load().values():   # approved but never reached (gateway down, another meeting ran over)
            if m.get("status") == "scheduled" and now > float(m.get("end") or 0) + rules.LEAVE_GRACE_SECONDS:
                store.update(m["key"], lambda r: dict(r, status="missed"))
                notify("Omni missed %s (%s): it could not join in time." % (_title(m), _when(m.get("start"))))

    def _join(self, m: Dict[str, Any], status: Dict[str, Any], now: float) -> None:
        if status.get("ok") and status.get("alive"):
            if not m.get("blocked_notified"):
                store.update(m["key"], lambda r: dict(r, blocked_notified=True))
                notify("Omni could not join %s yet: the bot is still in another meeting." % _title(m))
            return
        try:
            cleared = meetbot.clear_stale_profile_locks()
            if cleared:
                logger.warning("omni-meet: cleared Chrome's stale profile lock (%s)", ", ".join(cleared))
        except Exception:  # noqa: BLE001
            logger.exception("omni-meet: could not check Chrome's profile lock")
        problems = meetbot.check()
        if problems:
            store.update(m["key"], lambda r: dict(r, status="failed", error="; ".join(problems)))
            notify("Omni could not join %s: %s" % (_title(m), "; ".join(problems)))
            return
        if meetbot.signed_in_account() is not None and meetbot.verify_sign_in() is None and not m.get("signin_notified"):
            store.update(m["key"], lambda r: dict(r, signin_notified=True))
            notify("Omni's Google account is signed out On the agent host, so it is joining %s as a guest and must be "
                   "admitted. To fix it: at the agent host, run `hermes omni-meet auth`." % _title(m))
        mode = rules.wanted_mode(m)
        unmute, why = self._recording_plan(m)
        continuing = bool(m.get("rejoining") and m.get("recording"))
        if continuing and not unmute:
            self._minutes_stop(m["key"], now)
            continuing = False
        duration = float(m["end"]) + rules.LEAVE_GRACE_SECONDS - now
        res = meetbot.start(m["url"], mode=mode, duration_s=int(duration), guest_name=rules.GUEST_NAME,
                            unmute_audio=unmute)
        if not res.get("ok"):
            store.update(m["key"], lambda r: dict(r, status="failed", rejoining=False, error=str(res.get("error"))[:300]))
            notify("Omni could not join %s: %s" % (_title(m), str(res.get("error"))[:200]))
            return
        store.update(m["key"], lambda r: dict(r, status="joining", mode=mode, out_dir=res.get("out_dir"),
                                               joined_at=now, last_address_index=-1, solo_since=None, rejoining=False,
                                               recording=continuing, recording_planned=unmute, recording_blocked=why,
                                               minutes_starts=0, minutes_failures=0, minutes_next_try=None,
                                               minutes_checked_at=None))
        if why and m.get("recording_blocked_notified") != why:
            store.update(m["key"], lambda r: dict(r, recording_blocked_notified=why))
            voice = " with its voice on" if m.get("speak") else ""
            notify("Omni is in %s%s, but it is not recording: %s." % (_title(m), voice, why))

    # ── recording ──
    def _recording_plan(self, m: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not minutes.enabled():
            return False, None
        st, err = None, None
        try:
            st = minutes.status()
        except minutes.MinutesError as exc:
            err = exc
        return recording_plan(True, st, err, rules.wanted_mode(m) == "realtime")

    def _minutes_try_start(self, key: str, now: float) -> Optional[str]:
        """One start attempt (counted). None = recording; otherwise why not, for the owner."""
        m = store.update(key, lambda r: dict(r, minutes_starts=int(r.get("minutes_starts") or 0) + 1)) or {}
        try:
            res = minutes.start(title=_title(m), capture_mode=minutes.CAPTURE_MODE, event_id=m.get("event_id"),
                                attendees=_attendees(m) or None)
            mid = str(res.get("meeting_id") or "")
            if not mid:
                raise minutes.MinutesError("bad_response", "Minutes did not say which recording it started")
        except minutes.MinutesError as exc:
            if exc.code in ("busy", "timeout"):
                outcome = self._minutes_after_refusal(key, exc.code, now)
                if outcome is not None:
                    return outcome or None
            reason = minutes.describe(exc)

            def failed(r):
                fails = int(r.get("minutes_failures") or 0) + 1
                more = int(r.get("minutes_starts") or 0) < MINUTES_MAX_STARTS
                delay = MINUTES_RETRY_DELAYS[min(fails, len(MINUTES_RETRY_DELAYS)) - 1]
                return dict(r, recording=False, minutes_error=reason[:300], minutes_failures=fails,
                            minutes_next_try=(now + delay) if more else None)
            store.update(key, failed)
            return reason
        store.update(key, lambda r: dict(r, recording=True, minutes_meeting_ids=list(r.get("minutes_meeting_ids") or []) + [mid],
                                         minutes_started_at=now, minutes_checked_at=now, minutes_error=None,
                                         minutes_failures=0, minutes_next_try=None, minutes_waiting=False))
        return None

    def _minutes_after_refusal(self, key: str, code: str, now: float) -> Optional[str]:
        """After ``busy``/``timeout``: ``""`` = a recording we just started is running (adopted),
        a sentence = waiting for Minutes to finish processing (not a failure: the attempt is refunded),
        None = a real failure (handled by the caller)."""
        try:
            st = minutes.status()
        except minutes.MinutesError:
            return None
        mid = str(st.get("meeting_id") or "")
        ours = {i for r in store.load().values() for i in (r.get("minutes_meeting_ids") or [])}
        if code == "timeout" and st.get("recording") and mid and mid not in ours \
                and float(st.get("duration_s") or 0) <= MINUTES_ADOPT_MAX_AGE_SECONDS:
            store.update(key, lambda r: dict(r, recording=True, minutes_meeting_ids=list(r.get("minutes_meeting_ids") or []) + [mid],
                                             minutes_started_at=now, minutes_checked_at=now, minutes_error=None,
                                             minutes_failures=0, minutes_next_try=None, minutes_waiting=False))
            return ""
        if code == "busy" and not st.get("recording") and (st.get("processing") or st.get("starting")):
            store.update(key, lambda r: dict(r, recording=False, minutes_waiting=True,
                                             minutes_starts=max(0, int(r.get("minutes_starts") or 0) - 1),
                                             minutes_next_try=now + MINUTES_PROCESSING_WAIT_SECONDS))
            return ("Minutes is still processing the previous recording, so this one starts as soon as that "
                    "finishes")
        return None

    def _retry_note(self, key: str, now: float) -> str:
        m = store.load().get(key) or {}
        if m.get("minutes_next_try"):
            wait = max(1, int(round(float(m["minutes_next_try"]) - now)))
            return "Omni will try again in %d seconds." % wait if wait < 60 else "Omni will try again in a minute."
        return "Omni has stopped trying; the rest of this meeting is not being recorded."

    def _minutes_begin(self, key: str, now: float) -> Optional[str]:
        """Admitted: start the recording. Returns a sentence for the joined message, or None."""
        m = store.load().get(key) or {}
        if not (minutes.enabled() and m.get("recording_planned")):
            return None
        if m.get("recording"):
            current = (m.get("minutes_meeting_ids") or [None])[-1]
            try:
                st = minutes.status()
                if st.get("recording") and st.get("meeting_id") == current:
                    store.update(key, lambda r: dict(r, minutes_checked_at=now))
                    return "Minutes is still recording it."
            except minutes.MinutesError:
                pass
            store.update(key, lambda r: dict(r, recording=False))
        err = self._minutes_try_start(key, now)
        if err is None:
            return "Minutes is recording it."
        notify("Omni is in %s but Minutes is not recording: %s. %s" % (_title(m), err, self._retry_note(key, now)))
        return None

    def _minutes_watch(self, key: str, now: float) -> None:
        """While joined: a stopped recording is noticed within one check and restarted as a new segment."""
        m = store.load().get(key) or {}
        if not (minutes.enabled() and m.get("recording_planned")):
            return
        if m.get("recording"):
            if now - float(m.get("minutes_checked_at") or 0) < MINUTES_CHECK_SECONDS:
                return
            store.update(key, lambda r: dict(r, minutes_checked_at=now))
            current = (m.get("minutes_meeting_ids") or [None])[-1]
            try:
                st = minutes.status()
                if st.get("recording") and st.get("meeting_id") == current:
                    return
                problem = ("Minutes is recording something else now" if st.get("recording") else
                           "Minutes is no longer recording it")
            except minutes.MinutesError as exc:
                problem = minutes.describe(exc)
            store.update(key, lambda r: dict(r, recording=False, minutes_failures=0))
            if int(m.get("minutes_starts") or 0) >= MINUTES_MAX_STARTS:
                notify("Minutes stopped recording %s: %s. Omni has already restarted it %d times, so it is not "
                       "trying again." % (_title(m), problem, MINUTES_MAX_STARTS - 1))
                return
            err = self._minutes_try_start(key, now)
            notify("Minutes stopped recording %s: %s. %s" % (
                _title(m), problem, "Omni started a new recording." if err is None else
                "Omni could not start a new one: %s. %s" % (err, self._retry_note(key, now))))
            return
        due = m.get("minutes_next_try")
        if due and now >= float(due) and int(m.get("minutes_starts") or 0) < MINUTES_MAX_STARTS:
            err = self._minutes_try_start(key, now)
            if err is None:
                notify("Minutes is now recording %s." % _title(m))
            elif not (store.load().get(key) or {}).get("minutes_next_try"):
                notify("Omni gave up on recording %s: %s. The rest of this meeting is not being recorded."
                       % (_title(m), err))

    def _minutes_stop(self, key: str, now: float) -> None:
        """Every way out of a meeting: stop our recording (nothing of ours running is fine)."""
        m = store.load().get(key)
        if not m:
            return
        store.update(key, lambda r: dict(r or {}, minutes_next_try=None))
        if not (minutes.enabled() and m.get("recording")):
            return
        current = (m.get("minutes_meeting_ids") or [None])[-1]
        if current:
            try:
                minutes.stop(current)
            except minutes.MinutesError as exc:
                if exc.code not in minutes.STOP_TOLERATED:
                    logger.warning("omni-meet: Minutes stop failed: %s", exc)
        store.update(key, lambda r: dict(r, recording=False, recording_stopped_at=now, minutes_processing=True))

    def _update_busy(self, now: float) -> None:
        """busy.json while a meeting is joined or Minutes is processing one of ours; removed when idle."""
        try:
            meetings = store.load()
            keys = {k for k, m in meetings.items() if m.get("status") == "joined"}
            processing = [k for k, m in meetings.items() if m.get("minutes_processing")]
            if processing:
                still = self._still_processing(now)
                for k in processing:
                    stale = now - float(meetings[k].get("recording_stopped_at") or 0) > MINUTES_PROCESSING_MAX_SECONDS
                    if still is False or stale:
                        store.update(k, lambda r: dict(r, minutes_processing=False))
                    else:
                        keys.add(k)
            store.write_busy(sorted(keys), now)
        except Exception:  # noqa: BLE001
            logger.exception("omni-meet: could not update the busy hint")

    def _still_processing(self, now: float) -> Optional[bool]:
        """Minutes' ``processing`` flag, asked at most every 30 s (None = not asked this tick)."""
        if not minutes.enabled():
            return False
        if now - getattr(self, "_processing_checked_at", 0.0) < MINUTES_CHECK_SECONDS:
            return None
        self._processing_checked_at = now
        try:
            return bool(minutes.status().get("processing"))
        except minutes.MinutesError:
            return False

    def _tend(self, key: str, m: Dict[str, Any], status: Dict[str, Any], now: float) -> None:
        mine = status.get("ok") and status.get("url") == m.get("url") and status.get("alive")
        if not mine:
            reason = rules.exit_reason(m, status, meetbot.signed_in_account() is not None)
            self._minutes_stop(key, now)
            store.update(key, lambda r: dict(r, status="done", left_at=now, leave_reason=str(reason)[:200]))
            if m.get("status") == "joined":
                notify(self._summary(store.load().get(key, m), "left %s (%s)" % (_title(m), reason)))
            else:
                notify("Omni did not get into %s: %s." % (_title(m), reason))
            return
        if not rules.should_be_in(m, now) or m.get("status") == "cancelled":
            meetbot.stop(reason="scheduled end" if m.get("status") != "cancelled" else "attendance cancelled")
            self._minutes_stop(key, now)
            store.update(key, lambda r: dict(r, status="done" if r.get("status") != "cancelled" else "cancelled", left_at=now))
            notify(self._summary(store.load().get(key, m), "left %s" % _title(m)))
            return
        if rules.wanted_mode(m) != m.get("mode"):
            # Speaking approved (needs the voice path) or revoked (back to the silent microphone).
            meetbot.stop(reason="switching voice")
            # The recording is the Mac's audio, not the bot's: it keeps running across the rejoin (Minutes
            # would refuse a new start until this segment was processed). _join stops it only if the new
            # join cannot play the call.
            store.update(key, lambda r: dict(r, status="scheduled", rejoining=True))
            notify("Omni is rejoining %s %s." % (_title(m), "with its voice on" if m.get("speak") else "listening only"))
            return
        if status.get("lobby_waiting") and not m.get("lobby_notified"):
            store.update(key, lambda r: dict(r, lobby_notified=True))
            notify("Omni is waiting in the lobby of %s. Admit it from the meeting if you want it there." % _title(m))
        if status.get("in_call") and m.get("status") == "joining":
            store.update(key, lambda r: dict(r, status="joined", in_call_at=now))
            voice = ""
            if m.get("mode") == "realtime":
                voice = (" Its voice is on: it answers only when someone addresses it as Omni."
                         if status.get("realtime_ready") else
                         " Its voice could NOT start (%s); it is listening only." % (status.get("error") or "audio bridge unavailable"))
            recording = self._minutes_begin(key, now)
            notify("Omni joined %s.%s%s" % (_title(m), voice, (" " + recording) if recording else ""))
            m = store.load().get(key, m)
        if m.get("status") == "joined":
            if self._left_alone(key, m, now):
                return
            self._minutes_watch(key, now)
            m = store.load().get(key, m)
        if m.get("status") == "joined" and m.get("speak") and m.get("mode") == "realtime" and status.get("realtime_ready"):
            self._listen(key, m, now)

    def _left_alone(self, key: str, m: Dict[str, Any], now: float) -> bool:
        """Everyone else left: after ALONE_LEAVE_SECONDS alone, leave (and stop recording)."""
        if not alone(m.get("out_dir")):
            if m.get("solo_since"):
                store.update(key, lambda r: dict(r, solo_since=None))
            return False
        since = m.get("solo_since")
        if not since:
            store.update(key, lambda r: dict(r, solo_since=now))
            return False
        if now - float(since) < ALONE_LEAVE_SECONDS:
            return False
        meetbot.stop(reason="everyone left")
        self._minutes_stop(key, now)
        store.update(key, lambda r: dict(r, status="done", left_at=now, leave_reason="everyone left"))
        notify(self._summary(store.load().get(key, m), "left %s because everyone else had left" % _title(m)))
        return True

    def _listen(self, key: str, m: Dict[str, Any], now: float) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        caps = rules.parse_transcript(meetbot.transcript_text(m.get("out_dir")))
        found = rules.addressed_utterances(caps, self.own_names, local_seconds(now), int(m.get("last_address_index", -1)))
        said = [r["text"] for r in m.get("spoken", [])]
        found = [u for u in found if not any(_echo(u.text, s) for s in said)]
        # Owner decision 2026-11-13: Omni answers only the owner. Others naming it are noted, never answered.
        others = [u for u in found if not assistant.is_owner(u.speaker)]
        found = [u for u in found if assistant.is_owner(u.speaker)]
        if others:
            store.update(key, lambda r: dict(r, last_address_index=max([int(r.get("last_address_index", -1))] + [x.index for x in others])))
        handled = [h for h in m.get("handled", []) if now - float(h.get("at", 0)) < rules.HANDLED_WINDOW_SECONDS]
        fresh = [u for u in found if not any(h.get("speaker") == u.speaker and rules.same_request(u.text, h.get("text", ""))
                                             for h in handled)]
        if found:
            store.update(key, lambda r: dict(r, last_address_index=max(x.index for x in found)))
        if not fresh:
            return
        u = fresh[-1]
        spoken = m.get("spoken", [])
        if len(spoken) >= rules.MAX_REPLIES_PER_MEETING:
            return
        if spoken and now - float(spoken[-1].get("at", 0)) < rules.MIN_REPLY_GAP_SECONDS:
            return
        entry = {"speaker": u.speaker, "text": u.text[:300], "at": now}
        store.update(key, lambda r: dict(r, handled=(r.get("handled", []) + [entry])[-20:]))
        self._worker = threading.Thread(target=self.reply, args=(key, caps, u), name="omni-meet-reply", daemon=True)
        self._worker.start()

    def reply(self, key: str, caps: List[rules.Caption], u: rules.Caption) -> None:
        """The owner asked Omni something: a full Omni turn answers, the room decides what may be said."""
        m = store.load().get(key) or {}
        out_dir = m.get("out_dir")
        solo = is_solo(out_dir)
        said = [r["text"] for r in m.get("spoken", [])]
        record = {"at": time.time(), "to": u.speaker, "asked": u.text[:300], "solo": solo}
        prompt = assistant.turn_prompt(_title(m), caps, u.text, solo, said)
        result: Dict[str, Any] = {}
        worker = threading.Thread(target=lambda: result.update(text=ask_omni(prompt, assistant.session_id(key))),
                                  name="omni-meet-turn", daemon=True)
        worker.start()
        worker.join(assistant.ACK_AFTER_S)
        if worker.is_alive() and self._may_speak(key):
            self._speak(out_dir, assistant.pick(assistant.ACKS))       # a long turn: say it is working on it
        worker.join(assistant.TURN_TIMEOUT_S)
        raw = result.get("text")
        if raw is None:
            line = assistant.pick(assistant.TIMEOUT_LINES)
            if self._may_speak(key):
                self._speak(out_dir, line)
            store.update(key, lambda r: dict(r, held=(r.get("held", []) + [dict(record, text="", why=["no answer from Omni"])])[-50:]))
            notify("Omni could not answer in %s: \"%s\" (the assistant turn failed or timed out)." % (_title(m), u.text))
            return
        if raw.strip() == assistant.SILENT:
            store.update(key, lambda r: dict(r, silent=(r.get("silent", []) + [dict(record, reason="not a request")])[-50:]))
            return
        text = assistant.to_speech(raw, solo)
        problems = assistant.hard_problems(text, solo)
        if not problems and not solo:
            verdict = assistant.privacy_verdict(ask_model(
                "The owner asked: %s\n\nThe assistant is about to say:\n<<<\n%s\n>>>" % (u.text, text),
                assistant.PRIVACY_SYSTEM, assistant.PRIVACY_SCHEMA))
            if verdict:
                problems = [verdict]
        if not self._may_speak(key):
            problems = ["speaking is no longer approved"]
        if problems:
            avoid = [r.get("text") for r in (store.load().get(key) or {}).get("spoken", [])][-3:]
            line = assistant.pick(assistant.DEFLECTIONS, avoid)
            if self._may_speak(key):
                self._speak(out_dir, line)
                store.update(key, lambda r: dict(r, spoken=r.get("spoken", []) + [dict(record, text=line, deflection=True)]))
            store.update(key, lambda r: dict(r, held=(r.get("held", []) + [dict(record, text=text, why=problems)])[-50:]))
            notify("Omni held back in %s (you asked: \"%s\") — %s. The full answer:\n%s"
                   % (_title(m), u.text, "; ".join(problems), raw[:3000]))
            return
        seconds = self._speak(out_dir, text)
        if seconds is None:
            return
        store.update(key, lambda r: dict(r, spoken=r.get("spoken", []) + [dict(record, text=text, seconds=round(seconds, 1))]))
        notify("Omni said in %s (you asked: \"%s\"):\n%s" % (_title(m), u.text, text))

    def _may_speak(self, key: str) -> bool:
        current = store.load().get(key) or {}
        return bool(current.get("speak") and current.get("status") == "joined")

    def _speak(self, out_dir: Optional[str], text: str) -> Optional[float]:
        try:
            return say(out_dir, text)
        except Exception as exc:  # noqa: BLE001
            notify("Omni could not speak: %s" % exc)
            return None

    def _summary(self, m: Dict[str, Any], head: str) -> str:
        lines = ["Omni %s." % head]
        if m.get("spoken"):
            lines.append("It spoke %d time(s)." % len(m["spoken"]))
        if m.get("held"):
            lines.append("It held back %d reply(ies)." % len(m["held"]))
        if m.get("minutes_meeting_ids"):
            n = len(m["minutes_meeting_ids"])
            lines.append("Minutes recorded it%s." % ("" if n == 1 else " in %d parts" % n))
        if m.get("out_dir"):
            lines.append("Transcript: %s/transcript.txt" % m["out_dir"])
        return " ".join(lines)

    # ── naming speakers ──
    #
    # Stages (``naming.stage`` in the meeting's store record; each step is idempotent across restarts):
    #   waiting  — Minutes ``get`` every 30 s until every part is needs_review/finalized
    #   planned  — evidence + confidence + model judgement are stored; auto names, clips and the POST
    #              are still to do (``auto_done`` records each assign, so a restart repeats none)
    #   review   — posted (``review_id``, ``posted_at``): answers applied once each (``applied``), then
    #              export + close; corrections applied for 7 days
    #   finished / failed / skipped — nothing more to do

    #: Tests run the planning step inline.
    naming_async = True

    def _naming(self, now: float) -> None:
        if not speakers_enabled():
            return
        if self._naming_busy():
            return
        for key, m in store.load().items():
            if naming_due(m, now):
                mids = _naming_new_mids(m)
                old = m.get("naming") or {}
                earlier = list(old.get("done_mids") or []) + list(old.get("mids") or [])
                store.update(key, lambda r, mids=mids, earlier=earlier: dict(r, naming={
                    "stage": "waiting", "mids": mids, "done_mids": earlier, "since": now, "next_at": now}))
                m = store.load().get(key) or {}
            n = m.get("naming") or {}
            if n.get("stage") not in NAMING_LIVE_STAGES or now < float(n.get("next_at") or 0):
                continue
            if n["stage"] == "waiting":
                self._naming_wait(key, now)
            elif n["stage"] == "planned":
                self._naming_spawn(self._naming_post, key, now)
            else:
                self._naming_poll(key, now)
            if self._naming_busy():
                return

    def _naming_busy(self) -> bool:
        t = getattr(self, "_naming_thread", None)
        return t is not None and t.is_alive()

    def _naming_spawn(self, fn, *args) -> None:
        """The planning step can take a model call and many clips: off the tick thread, one at a time."""
        def run():
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                logger.exception("omni-meet: speaker naming step failed")
        if not self.naming_async:
            run()
            return
        self._naming_thread = threading.Thread(target=run, name="omni-meet-naming", daemon=True)
        self._naming_thread.start()

    def _naming_set(self, key: str, **changes: Any) -> Optional[Dict[str, Any]]:
        def change(r):
            if r is None:
                return None
            return dict(r, naming=dict(r.get("naming") or {}, **changes))
        return store.update(key, change)

    def _naming_trouble(self, key: str, now: float, why: str, give_up: bool = True) -> None:
        """Minutes or Prism could not be reached (or the recording is gone): retry on a later tick; after
        NAMING_GIVE_UP_SECONDS tell the owner once — and, before the review exists, give up."""
        m = store.load().get(key) or {}
        n = m.get("naming") or {}
        since = float(n.get("error_since") or now)
        if not n.get("error_since") or n.get("error") != why[:300]:      # once per problem, not every 30 s
            logger.warning("omni-meet: speaker naming for %s is waiting: %s", key, why)
        if now - since < NAMING_GIVE_UP_SECONDS:
            self._naming_set(key, error_since=since, error=why[:300], next_at=now + NAMING_POLL_SECONDS)
            return
        if give_up:
            self._naming_export_best_effort(key)
            self._naming_set(key, stage="failed", error=why[:300], next_at=None)
            notify("Omni could not name the speakers of %s: %s." % (_title(m), why))
            return
        if not n.get("error_notified"):
            notify("Omni cannot apply your speaker answers for %s yet: %s. It keeps trying." % (_title(m), why))
        self._naming_set(key, error_since=since, error=why[:300], error_notified=True,
                         next_at=now + NAMING_POLL_SECONDS)

    def _naming_ok(self, key: str) -> None:
        n = (store.load().get(key) or {}).get("naming") or {}
        if n.get("error_since") or n.get("error_notified"):
            self._naming_set(key, error_since=None, error=None, error_notified=False)

    # step 1: wait for Minutes to finish processing every part
    def _naming_wait(self, key: str, now: float) -> None:
        m = store.load().get(key) or {}
        n = m.get("naming") or {}
        segs, pending, gone, trouble = [], False, 0, None
        for mid in n.get("mids") or []:
            try:
                seg = minutes.get(mid)
            except minutes.MinutesError as exc:
                if exc.code == "not_found":
                    gone += 1
                else:
                    trouble = minutes.describe(exc)
                continue
            status = str((seg.get("meeting") or {}).get("status") or "")
            if status in speakers.DONE_STATUSES:
                segs.append((mid, seg))
            elif status in ("starting", "recording", "processing", ""):
                pending = True
            # failed / recorded: that part has no speakers to name
        if trouble:
            return self._naming_trouble(key, now, trouble)
        if pending:
            if now - float(n.get("since") or now) > MINUTES_PROCESSING_MAX_SECONDS:
                self._naming_set(key, stage="failed", error="Minutes was still processing", next_at=None)
                notify("Omni could not name the speakers of %s: Minutes was still processing it after %d hours."
                       % (_title(m), MINUTES_PROCESSING_MAX_SECONDS // 3600))
                return
            self._naming_set(key, error_since=None, next_at=now + NAMING_POLL_SECONDS)
            return
        if not segs:
            if gone:
                return self._naming_trouble(key, now, "Minutes no longer has its recording")
            self._naming_set(key, stage="skipped", error="no processed recording", next_at=None)
            return
        self._naming_ok(key)
        self._naming_set(key, next_at=now + NAMING_POLL_SECONDS)     # a second tick never re-plans meanwhile
        self._naming_spawn(self._naming_plan, key, segs, now)

    # steps 2–4: evidence, confidence, judgement
    def _naming_plan(self, key: str, segs: List[Tuple[str, Dict[str, Any]]], now: float) -> None:
        m = store.load().get(key) or {}
        try:
            caps = rules.parse_transcript(meetbot.transcript_text(m.get("out_dir")))
        except Exception:  # noqa: BLE001 — no captions is still a plan (voiceprints, attendees)
            caps = []
        owner = {"names": assistant.owner_names(), "email": gcal.OWNER_EMAIL}
        pl = speakers.plan(segs, caps, _attendees(m), owner, self.own_names)
        rows = pl["rows"]
        if not rows:
            self._naming_set(key, stage="skipped", error="no speakers", next_at=None)
            return
        if any(r["kind"] == "ask" for r in rows):
            data = ask_model(speakers.model_prompt(rows, pl["candidates"], owner["names"][0]),
                             speakers.MODEL_SYSTEM, speakers.MODEL_SCHEMA)
            speakers.apply_judgement(rows, pl["candidates"], data)
        kept = [{k: v for k, v in r.items() if k not in _NAMING_PRIVATE_FIELDS} for r in rows]
        self._naming_set(key, stage="planned", rows=kept, candidates=pl["candidates"],
                         held_at=pl["started_at"] or m.get("start") or now, auto_done=[], next_at=now)
        self._naming_post(key, now)

    def _naming_assign(self, mid: str, cluster_id: str, guess: Optional[Dict[str, Any]], learn: bool) -> Optional[bool]:
        """True = named; False = Minutes refused for good (logged); None = try again later."""
        if not guess or not guess.get("name"):
            return False
        try:
            minutes.assign(mid, cluster_id, guess["name"], guess.get("email"), learn=learn)
            return True
        except minutes.MinutesError as exc:
            err = exc
        if err.code == "bad_request" and learn:
            try:
                minutes.assign(mid, cluster_id, guess["name"], guess.get("email"), learn=False)
                return True
            except minutes.MinutesError as exc:
                err = exc
        if err.code in NAMING_RETRY_CODES:
            return None
        logger.warning("omni-meet: Minutes would not name a speaker (%s)", err.code)
        return False

    def _naming_export(self, key: str) -> bool:
        """Export every named part once (False = Minutes busy/unreachable: try again later)."""
        m = store.load().get(key) or {}
        n = m.get("naming") or {}
        done = set(n.get("exported_ids") or [])
        for mid in n.get("mids") or []:
            if mid in done or mid not in {r.get("mid") for r in n.get("rows") or []}:
                continue
            try:
                res = minutes.export(mid)
                if not res.get("exported"):
                    logger.warning("omni-meet: Minutes did not export a meeting (%s)", str(res.get("reason") or "")[:120])
            except minutes.MinutesError as exc:
                if exc.code in NAMING_RETRY_CODES:
                    return False
                logger.warning("omni-meet: Minutes export failed (%s)", exc.code)
            done.add(mid)
            self._naming_set(key, exported_ids=sorted(done))
        self._naming_set(key, exported=True)
        return True

    def _naming_export_best_effort(self, key: str) -> None:
        if (store.load().get(key) or {}).get("naming", {}).get("exported"):
            return
        try:
            self._naming_export(key)
        except Exception:  # noqa: BLE001
            logger.exception("omni-meet: export after giving up failed")

    def _naming_clip(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            res = minutes.clip(row["mid"], row["cluster_id"], NAMING_CLIP_SECONDS)
        except minutes.MinutesError as exc:
            if exc.code in NAMING_RETRY_CODES:
                raise _Retry(minutes.describe(exc)) from None
            return None          # not_found / bad_request (no timed speech) / internal (audio missing)
        data = minutes.read_clip(res)
        if not data:
            return None
        try:
            dur = round(float(res.get("duration_s") or 0), 1)
        except (TypeError, ValueError):
            dur = 0.0
        if not 0 < dur <= minutes.CLIP_MAX_DURATION_S:
            return None
        return {"mime": "audio/mp4", "base64": base64.b64encode(data).decode("ascii"), "durationS": dur}

    # steps 5–7 (and 9): auto names, clips, the review
    def _naming_post(self, key: str, now: float) -> None:
        m = store.load().get(key) or {}
        n = m.get("naming") or {}
        rows = n.get("rows") or []
        done = set(n.get("auto_done") or [])
        for r in rows:
            if r["kind"] != "auto" or r.get("preassigned") or r["id"] in done:
                continue
            res = self._naming_assign(r["mid"], r["cluster_id"], r.get("guess"), bool(r.get("learn")))
            if res is None:
                return self._naming_trouble(key, now, "Minutes did not take the speaker names")
            done.add(r["id"])
            self._naming_set(key, auto_done=sorted(done))
        asks = [r for r in rows if r["kind"] == "ask"]
        if not asks and not n.get("exported") and not self._naming_export(key):
            return self._naming_trouble(key, now, "Minutes did not export the meeting")
        clips: Dict[str, Dict[str, Any]] = {}
        budget = REVIEW_CLIP_BUDGET_BYTES
        try:
            for r in asks:
                c = self._naming_clip(r)
                if c and len(c["base64"]) <= budget:
                    clips[r["id"]] = c
                    budget -= len(c["base64"])
        except _Retry as exc:
            return self._naming_trouble(key, now, str(exc))
        body = speakers.review_body(speakers.meeting_key("omni-meet", key, (n.get("mids") or ["?"])[0]), _title(m),
                                    float(n.get("held_at") or now), rows, n.get("candidates") or [], clips)
        status, resp = hooks.post_review(body)
        review = (resp or {}).get("review") if isinstance((resp or {}).get("review"), dict) else {}
        if status in (200, 201, 409) and hooks.review_id_ok(review.get("id")):
            self._naming_set(key, stage="review", review_id=review["id"], posted_at=now, review_status=body["status"],
                             closed="done" if not asks else None, applied={}, error_since=None, error=None,
                             next_at=now + NAMING_POLL_SECONDS)
            return
        if status == 0 or status == 429 or status >= 500:
            return self._naming_trouble(key, now, "the Omni server did not take the speaker review")
        # Refused for good (validation, or a review with answers we never saw): the names Omni was sure of
        # are already in Minutes; export so the vault note has them.
        self._naming_export_best_effort(key)
        self._naming_set(key, stage="failed", error="review refused (%d)" % status, next_at=None)
        notify("Omni named the speakers of %s it was sure of, but the Omni server refused the review (%d)."
               % (_title(m), status))

    # step 8: apply answers, export, close; corrections for 7 days
    def _naming_poll(self, key: str, now: float) -> None:
        m = store.load().get(key) or {}
        n = m.get("naming") or {}
        posted = float(n.get("posted_at") or now)
        rows = {r["id"]: r for r in n.get("rows") or []}
        status, resp = hooks.get_review(str(n.get("review_id") or ""))
        if status == 404:
            self._naming_export_best_effort(key)
            self._naming_set(key, stage="finished", next_at=None)
            return
        review = resp.get("review") if status == 200 and isinstance((resp or {}).get("review"), dict) else None
        answers = review.get("answers") if review and isinstance(review.get("answers"), dict) else {}
        applied = dict(n.get("applied") or {})
        trouble = None if review is not None else "the Omni server did not answer"
        unapplied = False
        for rid in sorted(answers):
            r, ans = rows.get(rid), answers[rid]
            if r is None or not isinstance(ans, dict):
                continue
            sig = speakers.answer_signature(ans)
            if applied.get(rid) == sig:
                continue
            what, target = speakers.answer_target(r, ans)
            if what == "assign":
                res = self._naming_assign(r["mid"], r["cluster_id"], target,
                                          float(r.get("seconds") or 0) >= speakers.LEARN_MIN_SECONDS)
                if res is None:
                    trouble, unapplied = "Minutes did not take the speaker names", True
                    continue
            applied[rid] = sig
            self._naming_set(key, applied=dict(applied))
        complete = review is not None and speakers.review_complete(review, list(rows.values()))
        expired = now - posted >= REVIEW_ANSWER_SECONDS
        # Export after every answer is in Minutes — or at 24 h even if Prism is unreachable.
        if (complete or expired) and not n.get("exported") and not unapplied:
            if not self._naming_export(key):
                trouble = "Minutes did not export the meeting"
        n = (store.load().get(key) or {}).get("naming") or {}
        if (complete or expired) and n.get("exported") and not n.get("closed") and review is not None:
            want = "done" if complete else "expired"
            cst, _ = hooks.close_review(n["review_id"], want)
            if cst in (200, 404):
                self._naming_set(key, closed=want)
            else:
                trouble = trouble or "the Omni server did not close the review"
        n = (store.load().get(key) or {}).get("naming") or {}
        if now - posted >= REVIEW_CORRECTION_SECONDS:
            self._naming_export_best_effort(key)
            self._naming_set(key, stage="finished", next_at=None)
            return
        if trouble:
            return self._naming_trouble(key, now, trouble, give_up=False)
        self._naming_ok(key)
        self._naming_set(key, next_at=now + (NAMING_IDLE_POLL_SECONDS if n.get("closed") else NAMING_POLL_SECONDS))

    # ── thread ──
    def run(self) -> None:
        logger.warning("omni-meet: conductor running")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — never let one bad tick stop the loop
                logger.exception("omni-meet: tick failed")
            self._stop.wait(TICK_SECONDS)

    def stop(self) -> None:
        self._stop.set()


def _echo(heard: str, said: str) -> bool:
    """A caption that is Omni hearing itself (its own words, captioned under another name)."""
    a, b = " ".join(heard.lower().split()), " ".join(said.lower().split())
    return bool(a and b) and (a[:30] in b or b[:30] in a)
