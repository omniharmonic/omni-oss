"""omni-meet: Omni attends approved meetings and speaks only where, when and what it is allowed to.

Offline: no browser, no Google, no model, no network. ffmpeg (if installed) converts a real tone.

    python3 -m unittest tests.test_omni_meet -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "hermes/plugins/omni-meet"
PKG = "omni_meet_under_test"


def load():
    for n in [n for n in sys.modules if n == PKG or n.startswith(PKG + ".")]:
        del sys.modules[n]
    spec = importlib.util.spec_from_file_location(PKG, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)
    return mod


M = load()
from omni_meet_under_test import assistant, bot_launcher, conductor, gcal, googleauth, meetbot, minutes, rules, speech, store, tools  # noqa: E402


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


# ── rules ───────────────────────────────────────────────────────────────────

class Speech(unittest.TestCase):
    def test_allowed_lines(self):
        for ok in ("I'm Alex's AI assistant; I'm here to listen and take notes.",
                   "Alex will follow up with you directly.",
                   "Noted, I'll make sure he sees that.",
                   "So far you agreed to ship the draft on Friday and revisit pricing later."):
            self.assertEqual(rules.speech_problems(ok), [], ok)

    def test_refused_lines(self):
        cases = {
            "Reach him at owner@example.com.": "email address",
            "See https://example.com for details.": "a link",
            "Call 415 555 0199 tomorrow.": "phone number",
            "His wallet is 0x52908400098527886E.": "wallet",
            "It's in ~/notes/plan.md right now.": "file path",
            "**Yes**, absolutely.": "markdown",
            "He is free Thursday on his calendar.": "private area",
            "Alex will join the board, I promise.": "commits the owner",
            "One. Two. Three.": "sentences",
            "x" * 400: "longer",
            "He has a doctor appointment then.": "private area",
            "": "nothing",
        }
        for text, want in cases.items():
            problems = rules.speech_problems(text)
            self.assertTrue(any(want in p for p in problems), (text, problems))

    def test_parse_reply_fails_closed(self):
        self.assertEqual(rules.parse_reply(None)[0], False)
        self.assertEqual(rules.parse_reply({"speak": "yes", "text": "hi"})[0], False)
        self.assertEqual(rules.parse_reply({"speak": True, "text": "  "})[0], False)
        self.assertEqual(rules.parse_reply({"speak": True, "text": "Hi there.", "reason": "r"}), (True, "Hi there.", "r"))


class Addressing(unittest.TestCase):
    def caps(self, *lines):
        return rules.parse_transcript("\n".join(lines))

    def test_growing_caption_waits_to_settle_then_answers_final_text(self):
        t = self.caps("[10:00:00] Dana: Omni can",
                      "[10:00:01] Dana: Omni can you recap",
                      "[10:00:02] Dana: Omni can you recap the decisions?")
        now = 10 * 3600 + 3        # one second after the last line: still settling
        self.assertEqual(rules.addressed_utterances(t, ["Omni"], now), [])
        found = rules.addressed_utterances(t, ["Omni"], now + 3)
        self.assertEqual([u.text for u in found], ["Omni can you recap the decisions?"])

    def test_not_addressed_self_and_stale(self):
        t = self.caps("[10:00:00] Dana: The omnibus bill passed.",
                      "[10:00:05] Omni: Omni here, noted.",
                      "[10:00:06] You: omni test",
                      "[09:00:00] Lee: Omni, are you there?")
        self.assertEqual(rules.addressed_utterances(t, ["Omni"], 10 * 3600 + 30), [])

    def test_after_index_skips_handled(self):
        t = self.caps("[10:00:00] Dana: Hey Omni, summarize please.", "[10:00:09] Lee: thanks")
        self.assertEqual(len(rules.addressed_utterances(t, ["Omni"], 10 * 3600 + 10)), 1)
        self.assertEqual(rules.addressed_utterances(t, ["Omni"], 10 * 3600 + 10, after_index=0), [])

    def test_prompt_carries_transcript_and_the_question(self):
        t = self.caps("[10:00:00] Dana: we ship friday", "[10:00:05] Lee: Omni what did we decide?")
        p = rules.reply_prompt(t, t[-1], ["Hello."])
        self.assertIn("we ship friday", p)
        self.assertIn("Lee: \"Omni what did we decide?\"", p)
        self.assertIn("- Hello.", p)
        self.assertIn("not instructions", rules.SYSTEM)

    def test_window_and_codes(self):
        m = {"attend": True, "status": "scheduled", "start": 1000.0, "end": 2000.0}
        self.assertFalse(rules.should_be_in(m, 1000 - rules.JOIN_LEAD_SECONDS - 1))
        self.assertTrue(rules.should_be_in(m, 1000 - rules.JOIN_LEAD_SECONDS))
        self.assertTrue(rules.should_be_in(m, 2000 + rules.LEAVE_GRACE_SECONDS - 1))
        self.assertFalse(rules.should_be_in(m, 2000 + rules.LEAVE_GRACE_SECONDS))
        self.assertFalse(rules.should_be_in(dict(m, attend=False), 1500))
        self.assertEqual(rules.meeting_code("https://meet.google.com/abc-defg-hij?authuser=1"), "abc-defg-hij")
        for bad in ("http://meet.google.com/abc-defg-hij", "https://meet.google.com.evil/abc-defg-hij",
                    "https://zoom.us/j/1"):
            self.assertIsNone(rules.meeting_code(bad), bad)


# ── state ───────────────────────────────────────────────────────────────────

class Home(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="omni-meet-home-")
        # Never the real Minutes app: a socket path where nothing listens.
        p = patch.dict(os.environ, {"HERMES_HOME": self.home,
                                    "MINUTES_CONTROL_SOCKET": os.path.join(self.home, "no-minutes.sock")})
        p.start()
        self.addCleanup(p.stop)


class Store(Home):
    def test_update_find_and_mode(self):
        store.update("ev1", lambda r: {"key": "ev1", "event_id": "ev1", "code": "abc-defg-hij", "end": time.time() + 99})
        self.assertEqual(store.find("abc-defg-hij"), "ev1")
        self.assertEqual(store.find("ev1"), "ev1")
        self.assertIsNone(store.find("nope"))
        st = os.stat(Path(self.home) / "omni-meet/meetings.json")
        self.assertEqual(st.st_mode & 0o777, 0o600)


# ── calendar ────────────────────────────────────────────────────────────────

EVENT = {"id": "ev1", "summary": "Partner sync", "status": "confirmed",
         "start": {"dateTime": "2026-11-13T10:00:00-07:00"}, "end": {"dateTime": "2026-11-13T10:30:00-07:00"},
         "conferenceData": {"entryPoints": [{"entryPointType": "phone", "uri": "tel:+1"},
                                            {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}]},
         "organizer": {"email": "owner@example.com", "self": True},
         "attendees": [{"email": "dana@example.com"}]}


class Calendar(unittest.TestCase):
    def test_parse(self):
        ev = gcal.parse_event({"event": EVENT})
        self.assertEqual(ev["url"], "https://meet.google.com/abc-defg-hij")
        self.assertFalse(ev["omni_invited"])
        self.assertTrue(ev["organizer_is_owner"])
        self.assertEqual(ev["end"] - ev["start"], 1800)
        ev = gcal.parse_event(dict(EVENT, hangoutLink="https://meet.google.com/xyz-abcd-efg?hs=1",
                                   attendees=[{"email": "Agent@Example.com"}]))
        self.assertEqual(ev["url"], "https://meet.google.com/xyz-abcd-efg")
        self.assertTrue(ev["omni_invited"])

    def test_invite_is_exactly_add_attendee_without_notifications(self):
        seen = []
        with patch.object(gcal, "_run", side_effect=lambda a, timeout=60: seen.append(a) or ""):
            gcal.invite_omni("ev1")
        self.assertEqual(seen, [["calendar", "update", "primary", "ev1", "--account", "owner@example.com",
                                 "--add-attendee", "agent@example.com", "--send-updates", "none",
                                 "--gmail-no-send"]])
        with self.assertRaises(gcal.CalendarError):
            gcal.invite_omni("ev1; rm -rf /")

    def test_invite_retries_rate_limits_only(self):
        calls, slept = [], []
        def flaky(a, timeout=60):
            calls.append(a)
            if len(calls) < 3:
                raise gcal.CalendarError("gog failed: Google API error (403 rateLimitExceeded)")
            return ""
        with patch.object(gcal, "_run", flaky):
            gcal.invite_omni("ev1", sleep=slept.append)
        self.assertEqual((len(calls), slept), (3, [2.0, 5.0]))
        calls.clear()
        with patch.object(gcal, "_run", side_effect=gcal.CalendarError("403 forbiddenForNonOrganizer")):
            with self.assertRaises(gcal.CalendarError):
                gcal.invite_omni("ev1", sleep=slept.append)


# ── the bot ─────────────────────────────────────────────────────────────────

class Bot(unittest.TestCase):
    def test_spawn_rewritten_only_for_our_join(self):
        argv = ["/py", "-P", "-m", "plugins.google_meet.meet_bot"]
        self.assertEqual(meetbot.rewrite_argv(argv), argv)
        tok = meetbot._OURS.set(True)
        try:
            self.assertEqual(meetbot.rewrite_argv(argv), ["/py", "-P", meetbot.LAUNCHER])
            self.assertEqual(meetbot.rewrite_argv(["/py", "-m", "other"]), ["/py", "-m", "other"])
        finally:
            meetbot._OURS.reset(tok)
        calls = []
        proxy = meetbot._SubprocessProxy(types.SimpleNamespace(Popen=lambda a, *x, **k: calls.append(a), PIPE=-1))
        tok = meetbot._OURS.set(True)
        try:
            proxy.Popen(argv, stdout=1)
        finally:
            meetbot._OURS.reset(tok)
        self.assertEqual(calls, [["/py", "-P", meetbot.LAUNCHER]])
        self.assertEqual(proxy.PIPE, -1)

    def test_status_keys_normalized(self):
        s = meetbot.normalize_status({"ok": True, "alive": True, "inCall": True, "lobbyWaiting": False,
                                      "realtimeReady": True, "leaveReason": None, "outDir": "/x", "extra": 1})
        self.assertEqual(s["in_call"], True)
        self.assertEqual(s["realtime_ready"], True)
        self.assertNotIn("extra", s)

    def test_launcher_disables_the_conversational_voice(self):
        class RealtimeSession:
            def connect(self):
                raise AssertionError("would open OpenAI")

            def speak(self, text, timeout=30.0):
                raise AssertionError("would speak a model reply")

            def cancel_response(self):
                return True

            def close(self):
                pass
        mod = types.SimpleNamespace(RealtimeSession=RealtimeSession)
        self.assertTrue(bot_launcher.disable_conversational_voice(mod))
        s = RealtimeSession()
        s.connect()
        self.assertFalse(s.speak("say something")["ok"])
        self.assertFalse(s.cancel_response())

    def test_launcher_refuses_a_changed_shape(self):
        self.assertFalse(bot_launcher.disable_conversational_voice(types.SimpleNamespace()))
        self.assertFalse(bot_launcher.disable_conversational_voice(types.SimpleNamespace(RealtimeSession=type("R", (), {}))))

    def test_launcher_runs_listen_only_when_it_cannot_patch(self):
        # A real subprocess: no google_meet importable here, so the patch fails and the bot module is
        # asked for in transcribe mode.
        fake = Path(tempfile.mkdtemp())
        (fake / "plugins/google_meet").mkdir(parents=True)
        (fake / "plugins/__init__.py").write_text("")
        (fake / "plugins/google_meet/__init__.py").write_text("")
        (fake / "plugins/google_meet/meet_bot.py").write_text(
            "import os\nprint('MODE', os.environ.get('HERMES_MEET_MODE'))\n")
        env = dict(os.environ, PYTHONPATH=str(fake), HERMES_MEET_MODE="realtime")
        out = subprocess.run([sys.executable, str(PLUGIN / "bot_launcher.py")], capture_output=True, text=True, env=env)
        self.assertIn("MODE transcribe", out.stdout, out.stderr)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class Voice(unittest.TestCase):
    def test_tone_becomes_24k_mono_pcm_appended_to_the_bot_speaker(self):
        d = Path(tempfile.mkdtemp())
        tone = d / "tone.wav"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-ar", "44100", str(tone)], check=True)
        pcm = speech.to_pcm([str(tone)])
        self.assertAlmostEqual(len(pcm) / 2 / 24000, 1.5, delta=0.05)   # 1 s + 2 × 0.25 s padding
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            if "-list_devices" in argv:
                return types.SimpleNamespace(returncode=0, stderr=(
                    "[AudioToolbox @ 0x1] CoreAudio devices:\n"
                    "[AudioToolbox @ 0x1] [0]                  agent host Speakers, BuiltInSpeakerDevice\n"
                    "[AudioToolbox @ 0x1] [1]                  BlackHole 2ch, BlackHole2ch_UID\n"))
            return types.SimpleNamespace(returncode=0, stderr=b"")
        with self.assertRaises(speech.SpeechError):
            speech.play(d, pcm, run=run)                                # no voice path in this meeting
        (d / "speaker.pcm").write_bytes(b"")
        self.assertAlmostEqual(speech.play(d, pcm, run=run), 1.5, delta=0.05)
        argv, kw = calls[-1]
        self.assertEqual(argv[argv.index("-audio_device_index") + 1], "1", "BlackHole found by name in the OUTPUT list")
        self.assertEqual(kw["input"], pcm, "one-shot playback of exactly this clip")
        self.assertEqual((d / "speaker.pcm").stat().st_size, 0, "the stalled relay file is no longer fed")

    def test_missing_blackhole_is_a_clear_error(self):
        run = lambda argv, **kw: types.SimpleNamespace(returncode=0, stderr="[0] agent host Speakers, BuiltIn\n")
        with self.assertRaisesRegex(speech.SpeechError, "BlackHole"):
            speech.output_device_index(run=run)


# ── the conductor, through a whole meeting ──────────────────────────────────

class FakeBot:
    def __init__(self, out_dir):
        self.out_dir, self.alive, self.url, self.mode = str(out_dir), False, None, None
        self.flags, self.starts, self.stops, self.unmuted = {}, [], [], []

    def status(self):
        if not self.url:
            return {"ok": False}
        return dict({"ok": True, "alive": self.alive, "url": self.url, "out_dir": self.out_dir}, **self.flags)

    def start(self, url, *, mode, duration_s, guest_name, unmute_audio=False):
        self.starts.append((url, mode, duration_s, guest_name))
        self.unmuted.append(unmute_audio)
        self.alive, self.url, self.mode = True, url, mode
        self.flags = {"in_call": False}
        (Path(self.out_dir) / "transcript.txt").write_text("")
        if mode == "realtime":
            (Path(self.out_dir) / "speaker.pcm").write_bytes(b"")
        return {"ok": True, "out_dir": self.out_dir}

    def stop(self, reason):
        self.stops.append(reason)
        self.alive = False
        self.flags["leave_reason"] = reason

    def check(self):
        return []

    def transcript_text(self, out_dir):
        return (Path(out_dir) / "transcript.txt").read_text()


class FakeMinutes:
    """The Minutes control socket's behaviour, in process. ``fail[method]`` = errors to raise, in order."""

    def __init__(self):
        self.calls, self.fail, self.n = [], {}, 0
        #: Like the real app: refuse a start while anything is recording or still processing.
        self.strict = False
        self.st = {"recording": False, "meeting_id": None, "capture_mode": None, "processing": False, "ready": True,
                   "default_output_device": "BlackHole 16ch", "default_input_device": "BlackHole 2ch"}

    def _maybe_fail(self, method):
        if self.fail.get(method):
            raise self.fail[method].pop(0)

    def status(self):
        self.calls.append(("status",))
        self._maybe_fail("status")
        return dict(self.st)

    def start(self, title, capture_mode="system_audio", event_id=None, attendees=None):
        self.calls.append(("start", title, capture_mode, event_id, attendees))
        self._maybe_fail("start")
        if self.strict and (self.st["recording"] or self.st["processing"]):
            raise minutes.MinutesError("busy", "Minutes is busy")
        self.n += 1
        mid = "m%d" % self.n
        self.st.update(recording=True, meeting_id=mid, capture_mode=capture_mode)
        return {"meeting_id": mid, "capture_mode": capture_mode}

    def stop(self, meeting_id):
        self.calls.append(("stop", meeting_id))
        self._maybe_fail("stop")
        if self.st["meeting_id"] != meeting_id:
            raise minutes.MinutesError("wrong_meeting" if self.st["recording"] else "not_recording", "")
        self.st.update(recording=False, meeting_id=None, processing=True)
        return {"meeting_id": meeting_id, "processing": True}

    def named(self, method):
        return [c for c in self.calls if c[0] == method]


class Meeting(Home):
    def setUp(self):
        super().setUp()
        self.out = Path(tempfile.mkdtemp())
        self.bot = FakeBot(self.out)
        self.sent, self.said, self.model = [], [], {"speak": True, "text": "Alex will follow up with you directly.", "reason": "asked about schedule"}
        for name in ("status", "start", "stop", "check", "transcript_text"):
            p = patch.object(meetbot, name, getattr(self.bot, name))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(meetbot, "clear_stale_profile_locks", lambda: [])
        p.start()
        self.addCleanup(p.stop)
        self.minutes = FakeMinutes()
        for name in ("status", "start", "stop"):
            p = patch.object(minutes, name, getattr(self.minutes, name))
            p.start()
            self.addCleanup(p.stop)
        # The assistant turn (a real Omni turn in production) and the privacy review are faked here.
        self.answer, self.review, self.solo, self.prompts = "Here is the recap.", {"ok": True, "reason": ""}, False, []
        for target, fn, value in ((conductor, "notify", self.sent.append),
                                  (conductor, "ask_model", lambda prompt, system=None, schema=None: dict(self.review)),
                                  (conductor, "ask_omni", lambda prompt, session: (self.prompts.append(prompt), self.answer)[1]),
                                  (conductor, "is_solo", lambda d: self.solo),
                                  (conductor, "say", lambda d, t: self.said.append(t) or 1.0)):
            p = patch.object(target, fn, value)
            p.start()
            self.addCleanup(p.stop)
        self.t0 = time.time() + 3600
        store.update("ev1", lambda r: {"key": "ev1", "title": "Partner sync", "url": "https://meet.google.com/abc-defg-hij",
                                       "code": "abc-defg-hij", "start": self.t0, "end": self.t0 + 1800,
                                       "attend": True, "speak": False, "status": "scheduled"})
        self.c = conductor.Conductor()

    def run_reply(self):
        if self.c._worker is not None:
            self.c._worker.join(5)

    def caption(self, ts, who, text):
        with open(self.out / "transcript.txt", "a") as f:
            f.write("[%s] %s: %s\n" % (clock(ts), who, text))

    def test_listen_only_meeting_end_to_end(self):
        self.c.tick(self.t0 - 600)
        self.assertEqual(self.bot.starts, [])                             # not yet
        self.c.tick(self.t0 - 100)
        self.assertEqual(self.bot.starts[0][1], "transcribe")             # silent microphone
        self.assertEqual(self.bot.starts[0][3], "Omni")
        self.bot.flags["in_call"] = True
        self.c.tick(self.t0)
        self.assertEqual(store.load()["ev1"]["status"], "joined")
        self.assertIn("Omni joined Partner sync.", self.sent[-1])
        self.caption(self.t0, "Dana", "Omni what's Alex's schedule tomorrow?")
        self.c.tick(self.t0 + 10)
        self.run_reply()
        self.assertEqual(self.said, [])                                   # no speaking grant: never answers
        self.c.tick(self.t0 + 1800 + rules.LEAVE_GRACE_SECONDS + 1)
        self.assertEqual(store.load()["ev1"]["status"], "done")
        self.assertEqual(self.bot.stops, ["scheduled end"])
        self.assertIn("transcript.txt", self.sent[-1])

    def test_speaking_approved_mid_meeting_rejoins_with_voice_and_answers_when_addressed(self):
        self.c.tick(self.t0 - 100)
        self.bot.flags["in_call"] = True
        self.c.tick(self.t0)
        store.update("ev1", lambda r: dict(r, speak=True))
        self.c.tick(self.t0 + 5)
        self.assertEqual(self.bot.stops, ["switching voice"])
        self.c.tick(self.t0 + 6)
        self.assertEqual(self.bot.starts[-1][1], "realtime")
        self.bot.flags.update(in_call=True, realtime_ready=True)
        self.c.tick(self.t0 + 7)
        self.assertIn("Its voice is on", self.sent[-1])
        self.caption(self.t0 + 8, "Dana", "We should ship on Friday.")
        self.c.tick(self.t0 + 20)
        self.run_reply()
        self.assertEqual(self.said, [])                                   # nobody addressed Omni
        self.caption(self.t0 + 21, "Alex Morgan", "Omni, can you recap what we said?")
        self.c.tick(self.t0 + 22)                                         # still settling
        self.run_reply()
        self.assertEqual(self.said, [])
        self.c.tick(self.t0 + 25)
        self.run_reply()
        self.assertEqual(self.said, ["Here is the recap."])
        self.assertIn("Omni said in Partner sync", self.sent[-1])
        self.c.tick(self.t0 + 30)
        self.run_reply()
        self.assertEqual(len(self.said), 1)                               # one address, one answer

    def test_a_re_split_caption_of_the_same_question_is_answered_once(self):
        # Meet re-segments captions as it revises them (seen on the agent host 2026-11-13).
        store.update("ev1", lambda r: dict(r, speak=True))
        self.c.tick(self.t0 - 100)
        self.bot.flags.update(in_call=True, realtime_ready=True)
        self.c.tick(self.t0)
        self.caption(self.t0 + 1, "Alex Morgan", "Omni, can you hear me?")
        self.c.tick(self.t0 + 10)
        self.run_reply()
        self.assertEqual(len(self.said), 1)
        self.caption(self.t0 + 12, "Alex Morgan", "Can you hear me, Omni?")
        self.c.tick(self.t0 + 30)
        self.run_reply()
        self.assertEqual(len(self.said), 1, "the same question, re-punctuated: not answered again")
        self.caption(self.t0 + 31, "Alex Morgan", "Omni, what did we decide about Friday?")
        self.c.tick(self.t0 + 50)
        self.run_reply()
        self.assertEqual(len(self.said), 2, "a new question is answered")

    def test_meet_hearing_omni_as_im_new_and_a_separate_question(self):
        # Exactly what Meet captioned on 2026-11-13 when Alex said "Omni, what is this meeting about?"
        store.update("ev1", lambda r: dict(r, speak=True))
        self.c.tick(self.t0 - 100)
        self.bot.flags.update(in_call=True, realtime_ready=True)
        self.c.tick(self.t0)
        self.caption(self.t0 + 1, "Alex Morgan", "I'm new.")
        self.c.tick(self.t0 + 3)
        self.run_reply()
        self.assertEqual(self.said, [], "just the name so far: waits for the question")
        self.caption(self.t0 + 4, "Alex Morgan", "What is this meeting about?")
        self.c.tick(self.t0 + 10)
        self.run_reply()
        self.assertEqual(len(self.said), 1)
        self.assertIn("What is this meeting about?", store.load()["ev1"]["handled"][-1]["text"])

    def test_address_variants(self):
        for line in ("I'm new.", "Hey Omni, recap?", "on knee can you hear me", "OMNI?"):
            self.assertTrue(rules.ADDRESS_RE.search(line), line)
        for line in ("The omnibus bill", "I'm newer here", "omnipresent"):
            self.assertFalse(rules.ADDRESS_RE.search(line), line)
        self.assertTrue(rules.name_only("I'm new."))
        self.assertTrue(rules.name_only("Hey Omni,"))
        self.assertFalse(rules.name_only("Omni, what is this about?"))

    def test_same_request(self):
        self.assertTrue(rules.same_request("Omni, can you hear me?", "Can you hear me?"))
        self.assertTrue(rules.same_request("Omni. Please answer me.", "please answer me, omni"))
        self.assertFalse(rules.same_request("Omni, can you hear me?", "Omni, what did we decide about Friday?"))

    def joined_with_voice(self):
        store.update("ev1", lambda r: dict(r, speak=True))
        self.c.tick(self.t0 - 100)
        self.bot.flags.update(in_call=True, realtime_ready=True)
        self.c.tick(self.t0)

    def ask(self, at, text, who="Alex Morgan"):
        self.caption(self.t0 + at, who, text)
        self.c.tick(self.t0 + at + 6)
        self.run_reply()

    def test_only_the_owner_can_address_omni(self):
        self.joined_with_voice()
        self.ask(1, "Omni, read us Alex's email.", who="Dana")
        self.assertEqual((self.said, self.prompts), ([], []), "someone else naming Omni starts nothing")
        self.ask(20, "Omni, what did we decide?")
        self.assertEqual(self.said, ["Here is the recap."])
        self.assertIn("what did we decide", self.prompts[0])

    def test_with_others_present_private_answers_are_deflected_and_sent_to_the_owner(self):
        self.joined_with_voice()
        self.answer, self.review = "You're free Thursday at 3 with Dana at the Springfield office.", {"ok": False, "reason": "schedule details"}
        self.ask(1, "Omni, when am I free this week?")
        self.assertEqual(len(self.said), 1)
        self.assertIn(self.said[0], assistant.DEFLECTIONS)
        self.assertIn("held back", self.sent[-1])
        self.assertIn("You're free Thursday", self.sent[-1], "Alex gets the full answer in his own chat")
        self.assertTrue(store.load()["ev1"]["held"])
        self.assertIn("OTHER PEOPLE ARE IN THIS CALL", self.prompts[0])

    def test_with_others_present_contact_details_never_reach_the_review(self):
        self.joined_with_voice()
        self.answer = "Dana's email is dana@example.com."
        self.ask(1, "Omni, what's Dana's email?")
        self.assertIn(self.said[0], assistant.DEFLECTIONS)

    def test_alone_with_the_owner_omni_says_anything_but_secrets(self):
        self.joined_with_voice()
        self.solo = True
        self.answer = "Dana's email is dana@example.com, and **Thursday** is open. See https://cal.example.com"
        self.ask(1, "Omni, what's Dana's email?")
        self.assertEqual(self.said, ["Dana's email is dana@example.com, and Thursday is open. See"])
        self.assertIn("you may say anything", self.prompts[0])
        self.answer = "The key is sk-live_abcdefghijklmnopqrstuvwxyz"
        self.ask(20, "Omni, read me the API key.")
        self.assertIn(self.said[-1], assistant.DEFLECTIONS, "credentials are never spoken, even alone")

    def test_not_a_request_stays_silent(self):
        self.joined_with_voice()
        self.answer = "[SILENT]"
        self.ask(1, "I was telling Omni about it earlier.")
        self.assertEqual(self.said, [])
        self.assertEqual(store.load()["ev1"]["silent"][0]["reason"], "not a request")

    def test_a_slow_turn_gets_a_short_acknowledgement_and_a_failed_one_a_natural_line(self):
        self.joined_with_voice()
        slow = lambda prompt, session: (time.sleep(0.3), "Done: three tasks are open.")[1]
        with patch.object(conductor, "ask_omni", slow), patch.object(assistant, "ACK_AFTER_S", 0.05):
            self.ask(1, "Omni, how many tasks are open?")
        self.assertIn(self.said[0], assistant.ACKS)
        self.assertEqual(self.said[1], "Done: three tasks are open.")
        with patch.object(conductor, "ask_omni", lambda p, s: None):
            self.ask(20, "Omni, what's new in my vault?")
        self.assertIn(self.said[-1], assistant.TIMEOUT_LINES)
        self.assertIn("could not answer", self.sent[-1])

    def test_bot_exit_and_missed_meetings_are_reported(self):
        self.c.tick(self.t0 - 100)
        self.bot.alive = False
        self.bot.flags["leave_reason"] = "denied"
        self.c.tick(self.t0)
        self.assertEqual(store.load()["ev1"]["status"], "done")
        # Meet's "You can't join" before any knock is not a host decision: say what actually happened.
        self.assertIn("Omni did not get into Partner sync", self.sent[-1])
        self.assertIn("not signed in", self.sent[-1])
        self.assertNotIn("denied", self.sent[-1])
        store.update("ev2", lambda r: {"key": "ev2", "title": "Old one", "url": "https://meet.google.com/abc-defg-hij",
                                       "start": time.time() - 9000, "end": time.time() - 7200, "attend": True,
                                       "status": "scheduled"})
        self.c.tick(time.time())
        self.assertEqual(store.load()["ev2"]["status"], "missed")


# ── the tools ───────────────────────────────────────────────────────────────

class Tools(Home):
    def setUp(self):
        super().setUp()
        self.invited = []
        for target, name, value in ((gcal, "get_event", lambda e, c="primary": gcal.parse_event(dict(EVENT, id=e))),
                                    (gcal, "invite_omni", lambda e, c="primary": self.invited.append(e)),
                                    (meetbot, "check", lambda: []),
                                    (meetbot, "auth_state_path", lambda: Path("/nonexistent/auth.json"))):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        future = datetime.fromtimestamp(time.time() + 7200).astimezone()
        EVENT["start"] = {"dateTime": future.isoformat()}
        EVENT["end"] = {"dateTime": datetime.fromtimestamp(time.time() + 9000).astimezone().isoformat()}

    def call(self, fn, **args):
        return json.loads(fn(args))

    def test_attend_invites_omni_listening_only(self):
        r = self.call(tools.attend, event_id="ev1")
        self.assertTrue(r["success"], r)
        self.assertEqual(self.invited, ["ev1"])
        self.assertFalse(r["speaking"])
        self.assertIn("guest named Omni", r["readiness"]["note"])
        m = store.load()["ev1"]
        self.assertEqual((m["status"], m["speak"], m["invited"]), ("scheduled", False, True))

    def test_attend_survives_an_invite_it_cannot_change(self):
        with patch.object(gcal, "invite_omni", side_effect=gcal.CalendarError("not the organizer")):
            r = self.call(tools.attend, event_id="ev1")
        self.assertTrue(r["success"])
        self.assertIn("admitted", r["invite_note"])

    def test_attend_refuses_bad_input(self):
        self.assertFalse(self.call(tools.attend, url="https://zoom.us/j/1", start="2030-01-01T10:00", end="2030-01-01T11:00")["success"])
        self.assertFalse(self.call(tools.attend, url="https://meet.google.com/abc-defg-hij")["success"])
        with patch.object(gcal, "get_event", lambda e, c="primary": dict(gcal.parse_event(EVENT), url="")):
            self.assertFalse(self.call(tools.attend, event_id="ev1")["success"])

    def test_speech_needs_attendance_then_is_per_meeting(self):
        self.assertFalse(self.call(tools.allow_speech, meeting="ev1")["success"])
        self.call(tools.attend, event_id="ev1")
        self.assertTrue(self.call(tools.allow_speech, meeting="abc-defg-hij")["success"])
        self.assertTrue(store.load()["ev1"]["speak"])
        self.assertTrue(self.call(tools.cancel, meeting="ev1", scope="speech")["success"])
        self.assertFalse(store.load()["ev1"]["speak"])

    def test_say_refuses_outside_a_voiced_meeting_and_filters(self):
        self.assertFalse(self.call(tools.say, text="Hello everyone.")["success"])
        self.assertIn("will not say", self.call(tools.say, text="Email me at a@b.co")["error"])
        self.call(tools.attend, event_id="ev1")
        store.update("ev1", lambda r: dict(r, status="joined", mode="transcribe"))
        self.assertIn("not approved", self.call(tools.say, text="Hello everyone.")["error"])
        d = tempfile.mkdtemp()
        store.update("ev1", lambda r: dict(r, speak=True, mode="realtime", out_dir=d))
        with patch.object(speech, "say", lambda out, text: 1.2):
            r = self.call(tools.say, text="Alex is running five minutes late.")
        self.assertTrue(r["success"], r)
        self.assertEqual(store.load()["ev1"]["spoken"][-1]["text"], "Alex is running five minutes late.")

    def test_cancel_attendance_leaves(self):
        self.call(tools.attend, event_id="ev1")
        stops = []
        with patch.object(meetbot, "status", lambda: {"alive": True, "url": "https://meet.google.com/abc-defg-hij"}), \
                patch.object(meetbot, "stop", lambda reason: stops.append(reason)):
            self.assertTrue(self.call(tools.cancel, meeting="ev1", scope="attendance")["success"])
        self.assertEqual(stops, ["attendance cancelled"])
        self.assertEqual(store.load()["ev1"]["status"], "cancelled")


class Registration(Home):
    def test_registers_six_tools_in_the_omni_toolset_and_no_conductor_outside_the_gateway(self):
        got = []
        ctx = types.SimpleNamespace(register_tool=lambda **k: got.append((k["name"], k["toolset"])),
                                    register_cli_command=lambda **k: None)
        with patch.object(sys, "argv", ["hermes", "chat"]):
            M.register(ctx)
        self.assertEqual(sorted(n for n, _ in got), sorted(s["name"] for s, _, _ in tools.TOOLS))
        self.assertEqual({t for _, t in got}, {"omni"})
        self.assertIsNone(M._conductor)

    def test_buzz_processes_get_nothing(self):
        got = []
        ctx = types.SimpleNamespace(register_tool=lambda **k: got.append(k), register_cli_command=lambda **k: None)
        with patch.dict(os.environ, {"OMNI_HERMES_GUARD": "buzz"}):
            M.register(ctx)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()


# ── Omni's Google sign-in ───────────────────────────────────────────────────

OTHER = "someone.else@example.com"


class GoogleAuth(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.auth = self.dir / "auth.json"

    def test_accounts_are_read_in_order_default_first(self):
        body = ('["gaia.l.a.r",[["gaia.l.a",1,"Omni","agent@example.com","https://lh3/photo",1,1,0],'
                '["gaia.l.a",1,"Other","Someone.Else@Example.com","https://lh3/p2",0]]]')
        self.assertEqual(googleauth.accounts_in(body), ["agent@example.com", OTHER])
        self.assertEqual(googleauth.accounts_in('["gaia.l.a.r",[]]'), [])

    def test_browser_identity_matches_the_installed_chrome(self):
        ident = googleauth.user_agent("142.0.7444.60")
        self.assertIn("Macintosh", ident)
        self.assertIn("Chrome/142.0.0.0", ident)
        self.assertIsNone(googleauth.user_agent(None))
        self.assertEqual(bot_launcher.CHROME, str(googleauth.CHROME))
        self.assertEqual(bot_launcher.CHROME_APP, str(googleauth.CHROME_APP))
        self.assertEqual(bot_launcher.PROFILE_NAME, googleauth.PROFILE_NAME)
        self.assertEqual(bot_launcher.PROFILE_DROP_DEFAULTS, googleauth.PROFILE_DROP_DEFAULTS)

    def test_signed_in_follows_the_last_check_with_google(self):
        self.assertIsNone(googleauth.signed_in_account(self.auth))
        googleauth.profile_dir(self.auth).mkdir()
        self.assertEqual(googleauth.record(self.auth, [rules.AGENT_EMAIL, OTHER]), rules.AGENT_EMAIL)
        self.assertEqual(googleauth.signed_in_account(self.auth), rules.AGENT_EMAIL)
        self.assertEqual(oct(googleauth.meta_path(self.auth).stat().st_mode & 0o777), "0o600")
        # Google says signed out (2026-11-13: the session was revoked) -> never "signed in" again until re-checked
        self.assertIsNone(googleauth.record(self.auth, []))
        self.assertIsNone(googleauth.signed_in_account(self.auth))
        self.assertIsNone(googleauth.record(self.auth, [OTHER, rules.AGENT_EMAIL]), "another default account")
        self.assertNotIn(OTHER, googleauth.meta_path(self.auth).read_text(), "never stores another account")

    def test_sign_in_opens_plain_chrome_with_no_automation(self):
        calls = []

        class Proc:
            def wait(self, timeout=None):
                calls.append(("wait", timeout))
        with patch.object(googleauth, "chrome_available", lambda: True):
            googleauth.open_for_sign_in(self.auth, popen=lambda argv, **k: calls.append(argv) or Proc())
        argv = calls[0]
        self.assertEqual(argv[0], str(googleauth.CHROME))
        self.assertIn("--user-data-dir=%s" % googleauth.profile_dir(self.auth), argv)
        joined = " ".join(argv)
        for flag in ("remote-debugging", "enable-automation", "headless", "AutomationControlled"):
            self.assertNotIn(flag, joined, "Google refuses a driven browser: the sign-in window must be ordinary Chrome")
        self.assertEqual(calls[1], ("wait", googleauth.SIGN_IN_TIMEOUT_S))
        self.assertEqual(oct(googleauth.profile_dir(self.auth).stat().st_mode & 0o777), "0o700")

    def fake_playwright(self, body, ok=True, page_html=""):
        seen = {}

        class Resp:
            def __init__(self):
                self.ok = ok

            def text(self):
                return body

        class Page:
            def goto(self, url, **k):
                seen["page"] = url

            def content(self):
                return page_html

        def get(url, **k):
            seen["get"] = url
            return Resp()

        class Ctx:
            request = types.SimpleNamespace(get=get)

            def new_page(self):
                return Page()

            def storage_state(self):
                raise AssertionError("cookies are never exported")

            def close(self):
                seen["closed"] = True

        class Chromium:
            def launch_persistent_context(self, path, **kw):
                seen["launch"] = (path, kw)
                return Ctx()

        class PW:
            chromium = Chromium()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        googleauth.profile_dir(self.auth).mkdir(parents=True, exist_ok=True)
        return seen, PW

    def test_verify_checks_the_profile_with_google_and_removes_any_cookie_copy(self):
        self.auth.write_text('{"cookies": ["stale copy"]}')
        seen, PW = self.fake_playwright('[["gaia.l.a",1,"Omni","agent@example.com"]]')
        self.assertEqual(googleauth.verify(self.auth, PW), rules.AGENT_EMAIL)
        path, kw = seen["launch"]
        self.assertEqual(path, str(googleauth.profile_dir(self.auth)))
        self.assertIn("--use-mock-keychain", kw["ignore_default_args"], "Chrome must read its real Keychain key")
        self.assertEqual(seen["get"], googleauth.LIST_ACCOUNTS)
        self.assertFalse(self.auth.exists(), "a stale cookie copy is what got the session revoked")
        self.assertEqual(googleauth.signed_in_account(self.auth), rules.AGENT_EMAIL)
        self.assertTrue(seen["closed"])

    def test_verify_records_signed_out_and_never_echoes_another_account(self):
        for body, page, why in (('["%s","agent@example.com"]' % OTHER, "", "not Omni's"),
                                ("[]", "<html>Sign in</html>", "not signed in")):
            googleauth.record(self.auth, [rules.AGENT_EMAIL])
            _seen, PW = self.fake_playwright(body, page_html=page)
            with self.assertRaises(googleauth.AuthError, msg=why) as cm:
                googleauth.verify(self.auth, PW)
            self.assertNotIn(OTHER, str(cm.exception))
            self.assertIsNone(googleauth.signed_in_account(self.auth), why)

    def test_verify_falls_back_to_the_account_page(self):
        seen, PW = self.fake_playwright("", ok=False, page_html='<a aria-label="Google Account: Omni (agent@example.com)">')
        self.assertEqual(googleauth.verify(self.auth, PW), rules.AGENT_EMAIL)
        self.assertEqual(seen["page"], "https://myaccount.google.com/")

    def test_meetbot_points_the_bot_at_the_profile_only_when_signed_in(self):
        started = []
        fake_pm = types.SimpleNamespace(start=lambda url, **k: started.append(k) or {"ok": True})
        googleauth.profile_dir(self.auth).mkdir()
        with patch.object(meetbot, "pm", lambda: fake_pm), patch.object(meetbot, "auth_state_path", lambda: self.auth):
            meetbot.start("https://meet.google.com/abc-defg-hij", mode="transcribe", duration_s=600, guest_name="Omni")
            googleauth.record(self.auth, [rules.AGENT_EMAIL])
            meetbot.start("https://meet.google.com/abc-defg-hij", mode="transcribe", duration_s=600, guest_name="Omni")
        self.assertIsNone(started[0]["auth_state"])
        self.assertEqual(started[1]["auth_state"], str(self.auth))
        self.assertEqual(bot_launcher.profile_for(str(self.auth)), str(googleauth.profile_dir(self.auth)))
        self.assertIsNone(bot_launcher.profile_for(""))
        self.assertIsNone(bot_launcher.profile_for(str(self.dir / "elsewhere/auth.json")))

    def test_the_bot_runs_installed_chrome_as_itself(self):
        mac = "Mozilla/5.0 (Macintosh) Chrome/142.0.0.0"
        bot = types.SimpleNamespace(chromium_executable=lambda: "/pm/chromium",
                                    _CONTEXT_ARGS={"viewport": {"width": 1}, "user_agent": "Mozilla/5.0 (X11; Linux x86_64)"})
        chrome = self.dir / "Google Chrome"
        chrome.write_text("")
        chrome.chmod(0o755)
        with patch.object(bot_launcher, "chrome_user_agent", lambda: mac):
            self.assertTrue(bot_launcher.use_installed_chrome(bot, str(chrome)))
            self.assertEqual(bot.chromium_executable(), str(chrome))
            self.assertEqual(bot._CONTEXT_ARGS["user_agent"], mac)
            self.assertFalse(bot_launcher.use_installed_chrome(types.SimpleNamespace(), str(chrome)), "changed bot shape")
            self.assertFalse(bot_launcher.use_installed_chrome(bot, str(self.dir / "missing")), "no Chrome")

    def test_conductor_rechecks_with_google_before_joining_and_says_when_signed_out(self):
        sent = []
        googleauth.profile_dir(self.auth).mkdir()
        googleauth.record(self.auth, [rules.AGENT_EMAIL])
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HERMES_HOME": home}), \
                patch.object(meetbot, "auth_state_path", lambda: self.auth), \
                patch.object(meetbot, "verify_sign_in", lambda: googleauth.record(self.auth, [])), \
                patch.object(meetbot, "check", lambda: []), \
                patch.object(meetbot, "start", lambda url, **k: {"ok": True, "out_dir": home}), \
                patch.object(conductor, "notify", sent.append):
            store.update("ev9", lambda r: {"key": "ev9", "title": "Sync", "url": "https://meet.google.com/abc-defg-hij",
                                           "start": time.time() + 60, "end": time.time() + 1800, "attend": True, "status": "scheduled"})
            conductor.Conductor()._join(store.load()["ev9"], {}, time.time())
        self.assertTrue(any("signed out" in x for x in sent), sent)
        self.assertIsNone(googleauth.signed_in_account(self.auth))

    def test_exit_reason_never_invents_a_host_decision(self):
        denied = {"leave_reason": "denied"}
        self.assertIn("not signed in", rules.exit_reason({"status": "joining"}, denied, signed_in=False))
        self.assertIn("organization", rules.exit_reason({"status": "joining"}, denied, signed_in=True))
        self.assertIn("nobody admitted", rules.exit_reason({"status": "joining", "lobby_notified": True}, denied, True))
        self.assertEqual(rules.exit_reason({"status": "joined"}, {"leave_reason": "page_closed"}, True), "page_closed")


class LiveProfileLaunch(unittest.TestCase):
    def setUp(self):
        self.launched = []
        test = self

        class Ctx:
            def __init__(self):
                self.granted, self.closed = [], 0

            def grant_permissions(self, perms, origin=None):
                self.granted.append((perms, origin))

            def close(self):
                self.closed += 1

        class Chromium:
            def launch(self, **kw):
                test.launched.append(("launch", kw))
                return "fresh-browser"

            def launch_persistent_context(self, path, **kw):
                test.launched.append(("profile", path, kw))
                test.ctx = Ctx()
                return test.ctx

        class CM:
            def __enter__(self):
                return types.SimpleNamespace(chromium=Chromium())

            def __exit__(self, *a):
                return False
        self.api = types.ModuleType("playwright.sync_api")
        self.api.sync_playwright = CM
        pkg = types.ModuleType("playwright")
        pkg.sync_api = self.api
        self.mods = patch.dict(sys.modules, {"playwright": pkg, "playwright.sync_api": self.api})
        self.mods.start()
        self.addCleanup(self.mods.stop)
        self.out = tempfile.mkdtemp()

    def test_signed_in_launch_opens_the_profile_and_ignores_the_cookie_file(self):
        self.assertTrue(bot_launcher.patch_launch(self.out, "/p/omni-chrome-profile", "UA"))
        with self.api.sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chromium", executable_path="/chrome", headless=True, args=["--x"])
            ctx = browser.new_context(storage_state="/auth.json", permissions=["microphone", "camera"])
            ctx.close()
            browser.close()
        kind, path, kw = self.launched[0]
        self.assertEqual((kind, path, kw["executable_path"], kw["user_agent"]), ("profile", "/p/omni-chrome-profile", "/chrome", "UA"))
        self.assertIn("--use-mock-keychain", kw["ignore_default_args"])
        self.assertEqual(kw["args"], ["--x", "--use-file-for-fake-video-capture=%s/omni-black.y4m" % self.out])
        self.assertIs(ctx, self.ctx)
        self.assertEqual(self.ctx.granted, [(["microphone", "camera"], "https://meet.google.com")])
        self.assertGreaterEqual(self.ctx.closed, 1, "closed; a second close by browser.close() is harmless")

    def test_guest_launch_is_the_normal_browser_with_a_black_camera(self):
        self.assertTrue(bot_launcher.patch_launch(self.out, None, "UA"))
        with self.api.sync_playwright() as pw:
            self.assertEqual(pw.chromium.launch(args=["--use-fake-device-for-media-stream"], headless=True), "fresh-browser")
        self.assertEqual(self.launched[0][1]["args"], ["--use-fake-device-for-media-stream",
                                                       "--use-file-for-fake-video-capture=%s/omni-black.y4m" % self.out])
        self.assertNotIn("ignore_default_args", self.launched[0][1], "muted (Playwright's default) unless opted in")

    def test_call_audio_stays_muted_unless_exactly_opted_in(self):
        for value, unmuted in (("", False), ("0", False), ("true", False), ("yes", False), ("1", True), (" 1 ", True)):
            env = {bot_launcher.UNMUTE_ENV: value}
            self.assertEqual(bot_launcher.audio_unmuted(env), unmuted, value)
            self.assertEqual("--mute-audio" in bot_launcher.drop_defaults(["--x"], env), unmuted, value)
        self.assertFalse(bot_launcher.audio_unmuted({}))
        self.assertEqual(bot_launcher.drop_defaults(["--mute-audio"], {bot_launcher.UNMUTE_ENV: "1"}), ["--mute-audio"])
        self.assertEqual(bot_launcher.PROFILE_DROP_DEFAULTS, ["--use-mock-keychain", "--enable-automation"],
                         "the shared constant is never mutated")

    def test_opted_in_profile_launch_keeps_playwright_from_muting_the_call(self):
        with patch.dict(os.environ, {bot_launcher.UNMUTE_ENV: "1"}):
            self.assertTrue(bot_launcher.patch_launch(self.out, "/p/omni-chrome-profile", "UA"))
            with self.api.sync_playwright() as pw:
                pw.chromium.launch(executable_path="/chrome", headless=True, args=[])
        self.assertEqual(self.launched[0][2]["ignore_default_args"],
                         ["--use-mock-keychain", "--enable-automation", "--mute-audio"])

    def test_opted_in_guest_launch_keeps_playwright_from_muting_the_call(self):
        with patch.dict(os.environ, {bot_launcher.UNMUTE_ENV: "1"}):
            self.assertTrue(bot_launcher.patch_launch(self.out, None, "UA"))
            with self.api.sync_playwright() as pw:
                pw.chromium.launch(headless=True, args=[])
        self.assertEqual(self.launched[0][1]["ignore_default_args"], ["--mute-audio"])


class Captions(unittest.TestCase):
    def page(self, in_call=True, key_works=True):
        st = {"on": False, "pressed": [], "clicked": 0, "installed": False}

        class Loc:
            def __init__(self, n, click=None):
                self.n, self._click = n, click
                self.first = self

            def count(self):
                return self.n

            def is_visible(self):
                return self.n > 0

            def click(self, timeout=None):
                st["clicked"] += 1
                self._click()

        class Page:
            keyboard = types.SimpleNamespace(press=lambda k: (st["pressed"].append(k), key_works and st.update(on=not st["on"])))

            def locator(self, css):
                if "eave call" in css:
                    return Loc(1 if in_call else 0)
                if "Captions and translation" in css:
                    return Loc(1, lambda: st.update(on=not st["on"]))
                return Loc(0)

            def evaluate(self, js, *a):
                if js == bot_launcher.OMNI_CAPTION_OBSERVER_JS:
                    st["installed"] = True
                    return None
                if js == "!!window.__hermesMeetInstalled":
                    return st["installed"]
                if "aria-label=\"Captions\"" in js:
                    return st["on"]
                return []
        return Page(), st

    def keeper(self):
        t = {"now": 1000.0}
        return bot_launcher.CaptionKeeper(types.SimpleNamespace(), clock=lambda: t["now"]), t

    def test_turns_captions_on_once_and_never_toggles_them_off(self):
        # 2026-11-13: the first keeper did not recognise "on" and pressed c every tick, toggling captions off.
        page, st = self.page()
        k, t = self.keeper()
        k.tick(page)
        self.assertTrue(st["on"])
        self.assertTrue(st["installed"], "our observer is installed when the page has none")
        for _ in range(5):
            t["now"] += 10
            k.tick(page)
        self.assertTrue(st["on"], "captions stay on")
        self.assertEqual(st["pressed"], ["c"], "pressed exactly once")
        self.assertTrue(k.on)

    def test_waits_for_the_call_and_falls_back_to_the_button(self):
        page, st = self.page(in_call=False)
        k, t = self.keeper()
        k.tick(page)
        self.assertEqual((st["on"], st["pressed"]), (False, []))
        page, st = self.page(key_works=False)
        k, t = self.keeper()
        k.tick(page)
        t["now"] += 10
        k.tick(page)
        self.assertEqual(st["clicked"], 1, "the key did nothing: the second attempt clicks the button")
        self.assertTrue(st["on"])

    def test_the_drain_loop_sees_the_same_page_with_the_keeper_in_front_and_our_observer(self):
        page, st = self.page()
        seen = []
        bot = types.SimpleNamespace(_CAPTION_OBSERVER_JS="STOCK",
                                    _drain_loop=lambda p, *a: seen.append(p.evaluate("window.__hermesMeetDrain && window.__hermesMeetDrain()")))
        self.assertTrue(bot_launcher.keep_captions_on(bot))
        self.assertEqual(bot._CAPTION_OBSERVER_JS, bot_launcher.OMNI_CAPTION_OBSERVER_JS, "the bot installs our reader")
        bot._drain_loop(page, "cfg", "state", {}, {})
        self.assertTrue(st["on"], "the keeper ran before the drain")
        self.assertEqual(seen, [[]])
        self.assertFalse(bot_launcher.keep_captions_on(types.SimpleNamespace()))

    def test_reader_targets_the_current_meet_layout(self):
        js = bot_launcher.OMNI_CAPTION_OBSERVER_JS
        for needle in ('aria-label="Captions"', "div.nMcdL", ".NWpY1d", ".ygicle", "__hermesMeetDrain", "STABLE_MS"):
            self.assertIn(needle, js)


# ── Omni shows no video ─────────────────────────────────────────────────────

class Camera(unittest.TestCase):
    def test_black_frame_is_a_valid_y4m(self):
        p = bot_launcher.black_video(str(Path(tempfile.mkdtemp()) / "b.y4m"), 320, 180)
        data = Path(p).read_bytes()
        header, rest = data.split(b"FRAME\n", 1)
        self.assertTrue(header.startswith(b"YUV4MPEG2 W320 H180"))
        self.assertEqual(len(rest), 320 * 180 * 3 // 2)
        self.assertEqual(set(rest[:320 * 180]), {16})           # black luma

    def fake_page(self, start_on=True, clickable=True):
        state = {"on": start_on, "clicks": 0}

        class Loc:
            def __init__(self, present, on_click=None):
                self.present, self.on_click = present, on_click
                self.first = self

            def count(self):
                return 1 if self.present else 0

            def is_visible(self):
                return self.present

            def click(self, timeout=None):
                state["clicks"] += 1
                if self.on_click:
                    self.on_click()

        class Page:
            def locator(self, css):
                if "Turn off" in css:
                    return Loc(state["on"], (lambda: state.update(on=False)) if clickable else None)
                return Loc(not state["on"])
        return Page(), state

    def test_camera_is_turned_off_before_joining(self):
        page, state = self.fake_page()
        self.assertTrue(bot_launcher.camera_off(page, timeout=2, sleep=lambda s: None))
        self.assertFalse(state["on"])
        page, state = self.fake_page(start_on=False)
        self.assertTrue(bot_launcher.camera_off(page, timeout=2, sleep=lambda s: None))
        self.assertEqual(state["clicks"], 0, "already off: nothing pressed")
        page, state = self.fake_page(clickable=False)
        self.assertFalse(bot_launcher.camera_off(page, timeout=0.2, sleep=lambda s: time.sleep(0.05)))

    def test_join_is_wrapped_and_a_changed_bot_is_refused(self):
        calls = []
        bot = types.SimpleNamespace(_join=lambda page, cfg, state: calls.append("join"))
        page, state = self.fake_page()
        self.assertTrue(bot_launcher.keep_camera_off(bot))
        bot._join(page, None, None)
        self.assertEqual((calls, state["on"]), (["join"], False))
        self.assertFalse(bot_launcher.keep_camera_off(types.SimpleNamespace()))


class Assistant(unittest.TestCase):
    def test_the_question_becomes_an_omni_turn_on_hermes_api_server(self):
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "  Three tasks are open.  "}}]}).encode()

        def opener(req, timeout=None):
            seen.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data), timeout=timeout)
            return Resp()
        with patch.dict(os.environ, {"API_SERVER_KEY": "k" * 20, "API_SERVER_HOST": "0.0.0.0", "API_SERVER_PORT": "8642"}):
            out = assistant.ask_omni("prompt", assistant.session_id("3u7jo4r4u912e5vcu1g6jtl4o1"), opener=opener)
        self.assertEqual(out, "Three tasks are open.")
        self.assertEqual(seen["url"], "http://127.0.0.1:8642/v1/chat/completions")
        self.assertEqual(seen["headers"]["X-hermes-session-id"], "omni-meet-3u7jo4r4u912e5vcu1g6jtl4o1",
                         "one session per meeting: follow-ups keep context")
        self.assertEqual(seen["body"]["messages"][0]["content"], "prompt")
        with patch.dict(os.environ, {"API_SERVER_KEY": ""}):
            self.assertIsNone(assistant.ask_omni("p", "s", opener=opener), "no key: no turn")

    def test_owner_names(self):
        self.assertTrue(assistant.is_owner("Alex Morgan"))
        self.assertTrue(assistant.is_owner("alex morgan "))
        self.assertFalse(assistant.is_owner("Dana"))
        self.assertFalse(assistant.is_owner("You"))
        with patch.dict(os.environ, {"OMNI_MEET_OWNER_NAMES": "B. Life"}):
            self.assertTrue(assistant.is_owner("B. Life"))
            self.assertFalse(assistant.is_owner("Alex Morgan"))

    def test_speech_is_plain_and_capped(self):
        text = "## Summary\n- **First**, see [the note](https://x.io/a).\n- Second point. " + "More words here. " * 200
        spoken = assistant.to_speech(text, solo=False)
        self.assertTrue(spoken.startswith("Summary First, see the note."))
        self.assertNotIn("http", spoken)
        self.assertLessEqual(len(spoken), assistant.GROUP_MAX_CHARS)
        self.assertTrue(spoken.endswith("."))
        self.assertGreater(len(assistant.to_speech(text, solo=True)), len(spoken))

    def test_privacy_review_fails_closed(self):
        self.assertIsNone(assistant.privacy_verdict({"ok": True, "reason": ""}))
        self.assertIn("schedule", assistant.privacy_verdict({"ok": False, "reason": "schedule"}))
        self.assertEqual(assistant.privacy_verdict({"speak": False}), "privacy review unavailable")

    def test_presence(self):
        self.assertEqual(bot_launcher.presence_count({"tiles": 2, "badge": 2}), 2)
        self.assertIsNone(bot_launcher.presence_count({"tiles": 2, "badge": 3}), "readings disagree: unknown")
        self.assertEqual(bot_launcher.presence_count({"tiles": 0, "badge": 3}), 3)
        self.assertIsNone(bot_launcher.presence_count(None))
        d = tempfile.mkdtemp()
        self.assertFalse(conductor.is_solo(d), "unknown is never 'alone'")
        Path(d, conductor.PRESENCE_FILE).write_text(json.dumps({"count": 2, "at": time.time()}))
        self.assertTrue(conductor.is_solo(d))
        Path(d, conductor.PRESENCE_FILE).write_text(json.dumps({"count": 2, "at": time.time() - 300}))
        self.assertFalse(conductor.is_solo(d), "a stale reading is unknown")
        Path(d, conductor.PRESENCE_FILE).write_text(json.dumps({"count": 3, "at": time.time()}))
        self.assertFalse(conductor.is_solo(d))


# ── recording with Minutes (meeting-assistant spec, Phase 2) ────────────────

import socket as _socket  # noqa: E402
import threading as _threading  # noqa: E402


class FakeMinutesServer:
    """A Unix-socket server speaking the Minutes control protocol; ``handler(req) -> response dict | None``."""

    def __init__(self, path, handler):
        self.path, self.handler, self.requests = path, handler, []
        self.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(5)
        self.thread = _threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.split(b"\n")[0])
                self.requests.append(req)
                resp = self.handler(req)
                if resp is not None:
                    conn.sendall((json.dumps(dict(resp, id=req["id"])) + "\n").encode())

    def close(self):
        self.sock.close()


class MinutesSocket(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mc-")
        self.path = os.path.join(self.dir, "c.sock")
        p = patch.dict(os.environ, {"MINUTES_CONTROL_SOCKET": self.path})
        p.start()
        self.addCleanup(p.stop)

    def serve(self, handler):
        srv = FakeMinutesServer(self.path, handler)
        self.addCleanup(srv.close)
        return srv

    def test_socket_path_default_and_override(self):
        self.assertEqual(str(minutes.socket_path()), self.path)
        with patch.dict(os.environ, {"MINUTES_CONTROL_SOCKET": ""}):
            self.assertTrue(str(minutes.socket_path()).endswith("Library/Application Support/Minutes/control.sock"))

    def test_status_and_start_success(self):
        def handler(req):
            if req["method"] == "status":
                return {"ok": True, "result": {"recording": False, "ready": True, "default_output_device": "BlackHole 16ch"}}
            return {"ok": True, "result": {"meeting_id": "abc", "capture_mode": req["params"]["capture_mode"]}}
        srv = self.serve(handler)
        self.assertEqual(minutes.status()["default_output_device"], "BlackHole 16ch")
        res = minutes.start(title="Partner sync", event_id="ev1", attendees=[{"name": "Dana"}])
        self.assertEqual(res, {"meeting_id": "abc", "capture_mode": "system_audio"})
        self.assertEqual(srv.requests[1]["method"], "start")
        self.assertEqual(srv.requests[1]["params"], {"title": "Partner sync", "capture_mode": "system_audio",
                                                     "event_id": "ev1", "attendees": [{"name": "Dana"}]})
        self.assertNotEqual(srv.requests[0]["id"], srv.requests[1]["id"])

    def test_error_codes_are_typed(self):
        codes = iter(["busy", "permission_denied", "not_recording"])
        self.serve(lambda req: {"ok": False, "error": {"code": next(codes), "message": "nope"}})
        for call, code in ((lambda: minutes.start(title="x"), "busy"),
                           (lambda: minutes.start(title="x"), "permission_denied"),
                           (lambda: minutes.stop("m1"), "not_recording")):
            with self.assertRaises(minutes.MinutesError) as ctx:
                call()
            self.assertEqual((ctx.exception.code, ctx.exception.message), (code, "nope"))
        self.assertIn("permission", minutes.describe(minutes.MinutesError("permission_denied", "System audio")))

    def test_unavailable_when_nothing_listens(self):
        with self.assertRaises(minutes.MinutesError) as ctx:
            minutes.status()                                  # no socket file at all
        self.assertEqual(ctx.exception.code, "unavailable")
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.bind(self.path)                                     # a socket file nobody accepts on
        s.close()
        with self.assertRaises(minutes.MinutesError) as ctx:
            minutes.status()
        self.assertEqual(ctx.exception.code, "unavailable")
        self.assertIn("not running", minutes.describe(ctx.exception))

    def test_closed_without_answer(self):
        self.serve(lambda req: None)
        with self.assertRaises(minutes.MinutesError) as ctx:
            minutes.status()
        self.assertEqual(ctx.exception.code, "bad_response")

    def test_switch(self):
        self.assertTrue(minutes.enabled({}))
        self.assertTrue(minutes.enabled({"OMNI_MEET_MINUTES": "on"}))
        for off in ("off", "OFF", "0", "false", "no"):
            self.assertFalse(minutes.enabled({"OMNI_MEET_MINUTES": off}), off)


class RecordingPlan(unittest.TestCase):
    def test_matrix(self):
        ready_16 = {"ready": True, "default_output_device": "BlackHole 16ch"}
        ready_2 = {"ready": True, "default_output_device": "BlackHole 2ch"}
        plan = conductor.recording_plan
        self.assertEqual(plan(False, ready_16, None, False), (False, None))           # switched off: silent
        self.assertEqual(plan(True, ready_16, None, False), (True, None))
        self.assertEqual(plan(True, ready_16, None, True), (True, None))              # speaking, safe output
        self.assertEqual(plan(True, ready_2, None, False), (True, None))              # listening only: no echo
        unmute, why = plan(True, ready_2, None, True)                                 # speaking into its own mic
        self.assertFalse(unmute)
        self.assertIn("BlackHole 2ch", why)
        self.assertIn("BlackHole 16ch", why)
        self.assertFalse(plan(True, {"ready": True}, None, True)[0], "unknown output while speaking is unsafe")
        unmute, why = plan(True, None, minutes.MinutesError("unavailable", ""), False)
        self.assertFalse(unmute)
        self.assertIn("not running", why)
        unmute, why = plan(True, {"ready": False, "not_ready_reason": "no permission"}, None, False)
        self.assertEqual((unmute, "no permission" in why), (False, True))
        self.assertEqual(speech.VOICE_DEVICE, "BlackHole 2ch")


class SpawnScoping(unittest.TestCase):
    """The audio decision reaches only OUR bot spawn, never another subprocess of the gateway."""

    def setUp(self):
        self.seen = []
        outer = self

        class Real:
            def Popen(self, argv, *args, **kwargs):
                outer.seen.append((list(argv), kwargs.get("env")))
                return "proc"
        self.proxy = meetbot._SubprocessProxy(Real())
        self.bot_argv = ["/py", "-P", "-m", meetbot.BOT_MODULE]

    def test_unmute_only_inside_our_start(self):
        self.assertEqual(meetbot.UNMUTE_ENV, bot_launcher.UNMUTE_ENV)
        proxy, bot_argv = self.proxy, self.bot_argv

        class PM:
            def start(self, url, **kw):
                proxy.Popen(bot_argv, env={"PATH": "/bin", "OMNI_MEET_UNMUTE_AUDIO": "1"})
                proxy.Popen(["/bin/echo", "other"], env={"PATH": "/bin"})
                return {"ok": True}
        with patch.object(meetbot, "pm", lambda: PM()), patch.object(meetbot, "auth_state_path", lambda: Path("/nope")), \
                patch.object(meetbot, "signed_in_account", lambda: None):
            meetbot.start("https://meet.google.com/abc-defg-hij", mode="transcribe", duration_s=60, guest_name="Omni",
                          unmute_audio=True)
            meetbot.start("https://meet.google.com/abc-defg-hij", mode="realtime", duration_s=60, guest_name="Omni",
                          unmute_audio=False)
        (a1, e1), (o1, oe1), (a2, e2), _ = self.seen
        self.assertEqual(a1[-1], meetbot.LAUNCHER)
        self.assertEqual(e1.get("OMNI_MEET_UNMUTE_AUDIO"), "1")
        self.assertNotIn("OMNI_MEET_UNMUTE_AUDIO", e2, "muted: even an inherited 1 is removed")
        self.assertEqual((o1, oe1), (["/bin/echo", "other"], {"PATH": "/bin"}), "other spawns untouched")
        # Outside our start, the bot argv is not rewritten and the env is not touched.
        self.seen.clear()
        self.proxy.Popen(self.bot_argv)
        self.assertEqual(self.seen, [(self.bot_argv, None)])

    def test_bot_env_inherits_when_none(self):
        with patch.dict(os.environ, {"OMNI_MEET_UNMUTE_AUDIO": "1", "XYZ": "1"}):
            self.assertNotIn("OMNI_MEET_UNMUTE_AUDIO", meetbot.bot_env(None, False))
            self.assertEqual(meetbot.bot_env(None, False)["XYZ"], "1")
            self.assertEqual(os.environ["OMNI_MEET_UNMUTE_AUDIO"], "1", "the gateway's own env is not changed")


class StaleChromeLock(unittest.TestCase):
    def setUp(self):
        self.prof = Path(tempfile.mkdtemp(prefix="omni-prof-"))

    def lock(self, target):
        os.symlink(target, self.prof / "SingletonLock")
        os.symlink("12345", self.prof / "SingletonCookie")
        os.symlink("/nonexistent/SingletonSocket", self.prof / "SingletonSocket")

    def names(self):
        return sorted(p.name for p in self.prof.iterdir())

    def test_pure_rule(self):
        dead, alive = (lambda pid: False), (lambda pid: True)
        self.assertTrue(meetbot.stale_lock("omni-mini.local-4242", "omni-mini.local", dead))
        self.assertFalse(meetbot.stale_lock("omni-mini.local-4242", "omni-mini.local", alive))
        self.assertFalse(meetbot.stale_lock("laptop-4242", "omni-mini.local", dead))
        for junk in ("", "4242", "omni-mini.local-", "omni-mini.local-abc", "omni-mini.local-0"):
            self.assertFalse(meetbot.stale_lock(junk, "omni-mini.local", dead), junk)

    def test_dead_pid_on_this_host_is_cleared(self):
        self.lock("omni-mini-99999")
        removed = meetbot.clear_stale_locks(self.prof, hostname="omni-mini", pid_alive=lambda pid: False)
        self.assertEqual(sorted(removed), ["SingletonCookie", "SingletonLock", "SingletonSocket"])
        self.assertEqual(self.names(), [])

    def test_live_pid_or_other_host_is_kept(self):
        self.lock("omni-mini-%d" % os.getpid())
        self.assertEqual(meetbot.clear_stale_locks(self.prof, hostname="omni-mini"), [])   # really alive
        self.assertEqual(len(self.names()), 3)
        self.assertEqual(meetbot.clear_stale_locks(self.prof, hostname="elsewhere", pid_alive=lambda p: False), [])
        self.assertEqual(len(self.names()), 3)

    def test_no_lock_no_change(self):
        self.assertEqual(meetbot.clear_stale_locks(self.prof, hostname="x", pid_alive=lambda p: False), [])


class Recording(Meeting):
    def join(self, at=0, speak=False):
        if speak:
            store.update("ev1", lambda r: dict(r, speak=True))
        self.c.tick(self.t0 - 100 + at)
        self.bot.flags.update(in_call=True, realtime_ready=True)
        self.c.tick(self.t0 + at)

    def rec(self):
        return store.load()["ev1"]

    def busy(self):
        p = Path(self.home) / "omni-meet/busy.json"
        return json.loads(p.read_text()) if p.exists() else None

    def test_listening_meeting_is_unmuted_recorded_and_stopped_at_the_end(self):
        store.update("ev1", lambda r: dict(r, event_id="ev1", attendees=["dana@example.com", rules.AGENT_EMAIL]))
        self.join()
        self.assertEqual(self.bot.unmuted, [True])
        self.assertEqual(self.minutes.named("start"),
                         [("start", "Partner sync", "system_audio", "ev1", [{"email": "dana@example.com"}])])
        self.assertEqual((self.rec()["recording"], self.rec()["minutes_meeting_ids"]), (True, ["m1"]))
        self.assertIn("Minutes is recording it", self.sent[-1])
        self.c.tick(self.t0 + 1800 + rules.LEAVE_GRACE_SECONDS + 1)
        self.assertEqual(self.minutes.named("stop"), [("stop", "m1")])
        self.assertFalse(self.rec()["recording"])
        self.assertTrue(self.rec()["recording_stopped_at"])
        self.assertIn("Minutes recorded it", self.sent[-1])

    def test_speaking_into_blackhole_2ch_is_not_unmuted_and_says_why_once(self):
        self.minutes.st["default_output_device"] = "BlackHole 2ch"
        self.join(speak=True)
        self.assertEqual(self.bot.unmuted, [False])
        self.assertEqual(self.minutes.named("start"), [])
        warn = [t for t in self.sent if "not recording" in t]
        self.assertEqual(len(warn), 1)
        self.assertIn("with its voice on", warn[0])
        self.assertIn("BlackHole 16ch", warn[0])
        self.assertIn("echo", self.rec()["recording_blocked"])
        self.c.tick(self.t0 + 60)
        self.assertEqual(len([t for t in self.sent if "not recording" in t]), 1)

    def test_speaking_with_safe_output_is_recorded(self):
        self.join(speak=True)
        self.assertEqual(self.bot.unmuted, [True])
        self.assertEqual(len(self.minutes.named("start")), 1)

    def test_failed_start_notifies_and_retries_once_after_ten_seconds(self):
        self.minutes.fail["start"] = [minutes.MinutesError("permission_denied", "System audio recording is off")]
        self.join()
        self.assertIn("Omni is in Partner sync but Minutes is not recording", self.sent[-2])
        self.assertIn("permission", self.sent[-2])
        self.assertIn("10 seconds", self.sent[-2])
        self.assertFalse(self.rec()["recording"])
        self.c.tick(self.t0 + 3)
        self.assertEqual(len(self.minutes.named("start")), 1, "no tight loop")
        self.c.tick(self.t0 + 11)
        self.assertEqual(len(self.minutes.named("start")), 2)
        self.assertTrue(self.rec()["recording"])
        self.assertEqual(self.sent[-1], "Minutes is now recording Partner sync.")

    def test_repeated_failures_are_bounded(self):
        self.minutes.fail["start"] = [minutes.MinutesError("busy", "")] * 10
        self.join()
        for t in range(3, 600, 3):
            self.c.tick(self.t0 + t)
        self.assertEqual(len(self.minutes.named("start")), conductor.MINUTES_MAX_STARTS)
        self.assertIn("gave up", self.sent[-1])
        self.assertLessEqual(len([t for t in self.sent if "Minutes" in t]), 3)

    def test_minutes_quitting_mid_call_is_noticed_and_restarted_as_a_new_segment(self):
        self.join()
        self.c.tick(self.t0 + 20)
        self.assertEqual(len(self.minutes.named("status")), 1, "the join check only; no poll before 30 s")
        self.minutes.st.update(recording=False, meeting_id=None)          # Minutes quit and came back
        self.c.tick(self.t0 + 31)
        self.assertIn("Minutes stopped recording Partner sync", self.sent[-1])
        self.assertIn("started a new recording", self.sent[-1])
        self.assertEqual(self.rec()["minutes_meeting_ids"], ["m1", "m2"])
        # Recording someone else's meeting counts as stopped too.
        self.minutes.st.update(recording=True, meeting_id="other")
        self.c.tick(self.t0 + 62)
        self.assertIn("something else", self.sent[-1])

    def test_minutes_unreachable_mid_call(self):
        self.join()
        self.minutes.fail["status"] = [minutes.MinutesError("unavailable", "")]
        self.minutes.fail["start"] = [minutes.MinutesError("unavailable", "")]
        self.c.tick(self.t0 + 31)
        self.assertIn("not running", self.sent[-1])
        self.assertIn("could not start a new one", self.sent[-1])
        self.c.tick(self.t0 + 42)
        self.assertTrue(self.rec()["recording"])

    def test_bot_dying_stops_the_recording(self):
        self.join()
        self.bot.alive = False
        self.c.tick(self.t0 + 5)
        self.assertEqual(self.minutes.named("stop"), [("stop", "m1")])
        self.assertEqual(self.rec()["status"], "done")

    def test_cancelled_attendance_stops_the_recording(self):
        self.join()
        store.update("ev1", lambda r: dict(r, status="cancelled", attend=False))
        self.bot.stop("attendance cancelled")
        self.c.tick(self.t0 + 5)
        self.assertEqual(self.minutes.named("stop"), [("stop", "m1")])
        self.assertFalse(self.rec()["recording"])

    def test_mode_switch_keeps_the_same_recording_across_the_rejoin(self):
        self.minutes.strict = True
        self.join()
        store.update("ev1", lambda r: dict(r, speak=True))
        self.c.tick(self.t0 + 5)                                          # bot leaves to rejoin with a voice
        self.assertEqual(self.minutes.named("stop"), [], "the Mac's audio keeps recording")
        self.assertTrue(self.rec()["rejoining"])
        self.c.tick(self.t0 + 6)                                          # rejoin (16ch output: still unmuted)
        self.assertEqual((self.bot.starts[-1][1], self.bot.unmuted[-1]), ("realtime", True))
        self.assertTrue(self.rec()["recording"])
        self.bot.flags.update(in_call=True, realtime_ready=True)
        self.c.tick(self.t0 + 7)
        self.assertEqual(self.minutes.named("start")[1:], [], "no second start: Minutes would refuse it as busy")
        self.assertEqual((self.rec()["minutes_meeting_ids"], self.rec()["recording"]), (["m1"], True))
        self.assertIn("still recording", self.sent[-1])

    def test_mode_switch_into_an_echo_unsafe_join_stops_the_recording(self):
        self.join()
        self.minutes.st["default_output_device"] = "BlackHole 2ch"
        store.update("ev1", lambda r: dict(r, speak=True))
        self.c.tick(self.t0 + 5)
        self.c.tick(self.t0 + 6)                                          # the new join must stay muted
        self.assertEqual(self.bot.unmuted[-1], False)
        self.assertEqual(self.minutes.named("stop"), [("stop", "m1")])
        self.assertFalse(self.rec()["recording"])

    def test_busy_while_processing_waits_without_spending_attempts(self):
        self.minutes.strict = True
        self.minutes.st["processing"] = True                              # the previous meeting is still processing
        self.join()
        self.assertFalse(self.rec()["recording"])
        self.assertTrue(self.rec()["minutes_waiting"])
        self.assertEqual(self.rec()["minutes_starts"], 0, "waiting is not a failed attempt")
        self.assertTrue(any("still processing the previous recording" in t for t in self.sent), self.sent)
        for i in range(1, 8):                                             # minutes of processing: never gives up
            self.c.tick(self.t0 + 15 * i + 1)
        self.assertEqual(self.rec()["minutes_starts"], 0)
        self.assertFalse(any("gave up" in t for t in self.sent))
        self.minutes.st["processing"] = False
        self.c.tick(self.t0 + 15 * 9)
        self.assertEqual((self.rec()["recording"], self.rec()["minutes_meeting_ids"]), (True, ["m1"]))
        self.assertIn("now recording", self.sent[-1])

    def test_a_start_that_timed_out_but_began_is_adopted(self):
        def slow_start(*a, **k):
            self.minutes.calls.append(("start",) + a)
            self.minutes.st.update(recording=True, meeting_id="late1", duration_s=4)
            raise minutes.MinutesError("timeout", "Minutes did not answer in time")
        with patch.object(minutes, "start", slow_start):
            self.join()
        self.assertEqual((self.rec()["recording"], self.rec()["minutes_meeting_ids"]), (True, ["late1"]))
        self.assertIn("Minutes is recording it", self.sent[-1])

    def test_an_old_recording_is_never_adopted_after_a_timeout(self):
        def slow_start(*a, **k):
            self.minutes.calls.append(("start",) + a)
            self.minutes.st.update(recording=True, meeting_id="someone-elses", duration_s=900)
            raise minutes.MinutesError("timeout", "Minutes did not answer in time")
        with patch.object(minutes, "start", slow_start):
            self.join()
        self.assertFalse(self.rec()["recording"])
        self.assertNotIn("someone-elses", self.rec().get("minutes_meeting_ids") or [])

    def test_stop_tolerates_nothing_to_stop(self):
        self.join()
        self.minutes.st.update(recording=False, meeting_id=None)
        self.bot.alive = False
        self.c.tick(self.t0 + 5)                                          # stop -> not_recording: fine
        self.assertEqual(self.rec()["status"], "done")
        self.assertFalse(self.rec()["recording"])

    def presence(self, count):
        (self.out / "omni-presence.json").write_text(json.dumps({"count": count, "at": time.time()}))

    def test_alone_for_two_minutes_leaves_and_stops_recording(self):
        self.join()
        self.presence(1)
        self.c.tick(self.t0 + 10)
        self.c.tick(self.t0 + 100)
        self.assertEqual(self.bot.stops, [])
        self.presence(3)                                                  # someone came back: clock resets
        self.c.tick(self.t0 + 110)
        self.presence(1)
        self.c.tick(self.t0 + 120)
        self.c.tick(self.t0 + 200)
        self.assertEqual(self.bot.stops, [])
        self.c.tick(self.t0 + 241)
        self.assertEqual(self.bot.stops, ["everyone left"])
        self.assertEqual(self.minutes.named("stop"), [("stop", "m1")])
        self.assertEqual(self.rec()["leave_reason"], "everyone left")
        self.assertIn("everyone else had left", self.sent[-1])

    def test_busy_file_lifecycle(self):
        self.assertIsNone(self.busy())
        self.join()
        b = self.busy()
        self.assertEqual(b["meeting_keys"], ["ev1"])
        self.assertEqual(os.stat(Path(self.home) / "omni-meet/busy.json").st_mode & 0o777, 0o600)
        since = b["since"]
        self.c.tick(self.t0 + 1800 + rules.LEAVE_GRACE_SECONDS + 1)       # left; Minutes is processing
        self.assertEqual(self.busy(), {"since": since, "meeting_keys": ["ev1"]})
        self.minutes.st["processing"] = False
        self.c.tick(self.t0 + 1800 + rules.LEAVE_GRACE_SECONDS + 40)
        self.assertIsNone(self.busy())
        self.assertFalse(self.rec()["minutes_processing"])

    def test_switched_off_makes_no_minutes_calls_and_keeps_chrome_muted(self):
        with patch.dict(os.environ, {"OMNI_MEET_MINUTES": "off"}), \
                patch.object(minutes, "call", side_effect=AssertionError("socket call while off")):
            self.join()
            self.c.tick(self.t0 + 31)
            self.c.tick(self.t0 + 1800 + rules.LEAVE_GRACE_SECONDS + 1)
        self.assertEqual(self.minutes.calls, [])
        self.assertEqual(self.bot.unmuted, [False])
        self.assertFalse([t for t in self.sent if "Minutes" in t or "recording" in t])
        self.assertIsNone(self.busy())
