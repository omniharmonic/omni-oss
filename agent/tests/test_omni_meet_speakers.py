"""omni-meet speaker naming (speaker-review contract): speakers.py, the Minutes clip/assign/export client,
the Prism hook client, and the conductor's ``naming`` stage — all offline, with synthetic meetings.

Minutes and Prism are faked in process; no model, no network beyond a loopback test server.

    python3 -m unittest tests.test_omni_meet_speakers -v
"""

from __future__ import annotations

import base64
import http.server
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "hermes/plugins/omni-meet"
PKG = "omni_meet_speakers_under_test"


def load():
    for n in [n for n in sys.modules if n == PKG or n.startswith(PKG + ".")]:
        del sys.modules[n]
    spec = importlib.util.spec_from_file_location(PKG, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)
    return mod


load()
from omni_meet_speakers_under_test import conductor, gcal, hooks, minutes, rules, speakers, store  # noqa: E402

M4A = b"\x00\x00\x00\x18ftypM4A \x00\x00\x02\x00"         # an MP4 file starts with an ftyp box
OWNER = {"names": ["Alex Morgan", "Alex"], "email": "owner@example.com"}
OWN = ["Omni"]


def cap(i, clock_s, who, text):
    h, r = divmod(clock_s, 3600)
    m, s = divmod(r, 60)
    return rules.Caption(i, "%02d:%02d:%02d" % (h, m, s), clock_s, who, text)


def words(n, word="word"):
    return " ".join([word] * n)


# ── step 2a: captions on the recording's time line ──────────────────────────

@unittest.skipIf(ZoneInfo is None, "zoneinfo unavailable")
class CaptionClock(unittest.TestCase):
    def test_local_clock_in_the_injected_timezone(self):
        den = ZoneInfo("America/Edmonton")
        start = datetime(2026, 11, 22, 10, 0, 0, tzinfo=den).timestamp()
        self.assertEqual(speakers.caption_epoch(10 * 3600 + 300, start, den) - start, 300)
        # The same instant seen from Tokyo has a different wall clock, and the same offset.
        tok = ZoneInfo("Asia/Tokyo")
        clock = datetime.fromtimestamp(start + 300, tok)
        secs = clock.hour * 3600 + clock.minute * 60 + clock.second
        self.assertEqual(speakers.caption_epoch(secs, start, tok) - start, 300)

    def test_meeting_over_midnight(self):
        den = ZoneInfo("America/Edmonton")
        start = datetime(2026, 11, 22, 23, 50, 0, tzinfo=den).timestamp()
        self.assertEqual(speakers.caption_epoch(5 * 60, start, den) - start, 15 * 60)          # 00:05 next day
        self.assertEqual(speakers.caption_epoch(23 * 3600 + 45 * 60, start, den) - start, -5 * 60)  # before start

    def test_dst_change_during_the_meeting(self):
        den = ZoneInfo("America/Edmonton")
        start = datetime(2026, 3, 8, 1, 50, 0, tzinfo=den).timestamp()        # 01:50 MST; 02:00 → 03:00 MDT
        self.assertEqual(speakers.caption_epoch(3 * 3600 + 5 * 60, start, den) - start, 15 * 60)

    def test_spans_skip_omni_and_collapse_growing_lines(self):
        den = ZoneInfo("America/Edmonton")
        start = datetime(2026, 11, 22, 10, 0, 0, tzinfo=den).timestamp()
        base = 10 * 3600
        caps = [cap(0, base + 20, "Ana Lima", "Hello"), cap(1, base + 21, "Ana Lima", "Hello there " + words(8)),
                cap(2, base + 30, "You", words(5)), cap(3, base + 31, "Omni", words(5)),
                cap(4, base + 40, "Dana", words(10))]
        spans = speakers.caption_spans(caps, start, OWN, den)
        self.assertEqual([s["name"] for s in spans], ["Ana Lima", "Dana"])
        ana = spans[0]                                                       # 10 words, clock offset 21
        self.assertAlmostEqual(ana["start"], 21 - speakers.CAPTION_LAG_S - 10 / speakers.WORDS_PER_SECOND)
        self.assertAlmostEqual(ana["end"], 21 + speakers.CAPTION_TAIL_S)

    def test_votes_are_overlap_seconds(self):
        spans = [{"name": "A", "start": 0.0, "end": 10.0}, {"name": "B", "start": 8.0, "end": 20.0}]
        votes = speakers.caption_votes(spans, [{"start": 5, "end": 12}, {"start": 30, "end": 40}, {"start": "x"}])
        self.assertEqual(votes, {"A": 5.0, "B": 4.0})


# ── step 2b: candidates and names ───────────────────────────────────────────

class Names(unittest.TestCase):
    def setUp(self):
        self.people = speakers.attendee_candidates(
            [{"email": "ana.lima@example.com"}, {"email": "dana@example.com", "name": "Dana Ruiz"},
             {"email": "dan.ko@example.com"}, {"email": "owner@example.com"}, {"email": "not-an-email"}], OWNER)

    def test_attendees(self):
        self.assertEqual([(p["name"], p["email"]) for p in self.people],
                         [("Alex Morgan", "owner@example.com"), ("Ana Lima", "ana.lima@example.com"),
                          ("Dana Ruiz", "dana@example.com"), ("Dan Ko", "dan.ko@example.com")])

    def test_link_full_name_first_name_and_owner_aliases(self):
        link = lambda n: (speakers.link_name(n, self.people, OWNER) or {}).get("email")
        self.assertEqual(link("ana lima"), "ana.lima@example.com")
        self.assertEqual(link("Ana"), "ana.lima@example.com")                # unique first name
        self.assertEqual(link("Dana R."), "dana@example.com")
        self.assertEqual(link("Alex"), "owner@example.com")
        self.assertIsNone(link("Sam Rivers"))
        two = self.people + [{"name": "Ana Souza", "email": "as@example.com"}]
        self.assertIsNone(speakers.link_name("Ana", two, OWNER))              # ambiguous first name

    def test_caption_spelling_replaces_a_name_made_from_the_address(self):
        p = speakers.caption_person("Ana Lima-Costa", self.people, OWNER)
        self.assertEqual((p["name"], p["email"]), ("Ana Lima-Costa", "ana.lima@example.com"))
        c = speakers.candidates(self.people, [p, {"name": "Sam", "email": None}],
                                [{"name": "Dana Ruiz", "email": "dana@example.com"}])
        self.assertEqual([x["name"] for x in c], ["Alex Morgan", "Ana Lima-Costa", "Dana Ruiz", "Dan Ko", "Sam"])
        self.assertNotIn("email", c[-1])
        many = [{"name": "P%d" % i, "email": "p%d@example.com" % i} for i in range(50)]
        self.assertEqual(len(speakers.candidates(many, [], [])), speakers.MAX_CANDIDATES)

    def test_same_person(self):
        sp = speakers.same_person
        self.assertTrue(sp({"name": "Ana"}, {"name": "Ana Lima"}))
        self.assertFalse(sp({"name": "Ana Lima"}, {"name": "Ana Souza"}))
        self.assertTrue(sp({"name": "X", "email": "a@example.com"}, {"name": "Y", "email": "A@example.com"}))
        self.assertFalse(sp({"name": "Ana", "email": "a@example.com"}, {"name": "Ana", "email": "b@example.com"}))

    def test_voiceprint_only_for_auto_matches_with_a_name(self):
        vp = speakers.voiceprint_person
        self.assertIsNone(vp({"assignment": "suggested", "person_id": "p", "label": "Ana", "confidence": 0.9}))
        self.assertIsNone(vp({"assignment": "auto", "person_id": None, "label": "Ana"}))
        self.assertIsNone(vp({"assignment": "auto", "person_id": "p", "label": "Speaker 2"}))
        self.assertEqual(vp({"assignment": "auto", "person_id": "p", "label": "Ana", "confidence": 0.8})["confidence"], 0.8)


# ── step 3: confidence ──────────────────────────────────────────────────────

def ev(top=None, share=0.0, covered=0.0, vp=None, conf=0.0, owner_evidence=False):
    return {"top": {"name": top, "email": None} if top else None, "share": share, "covered": covered,
            "voiceprint": {"name": vp, "email": None, "confidence": conf} if vp else None,
            "owner_evidence": owner_evidence}


class Confidence(unittest.TestCase):
    J = staticmethod(speakers.judge)

    def test_high_from_captions(self):
        c, g, reasons = self.J(ev("Ana", 0.8, 30))
        self.assertEqual((c, g), ("high", {"name": "Ana"}))
        self.assertIn("80%", reasons[0])
        self.assertEqual(self.J(ev("Ana", 0.6, 10))[0], "high")                         # boundaries inclusive
        self.assertEqual(self.J(ev("Ana", 0.8, 9.9))[0], "medium")                       # too little caption time
        self.assertEqual(self.J(ev("Ana", 0.59, 30))[0], "medium")
        self.assertEqual(self.J(ev("Ana", 0.8, 30, vp="Ana Lima", conf=0.5))[0], "high")  # voiceprint agrees

    def test_high_from_the_voiceprint(self):
        self.assertEqual(self.J(ev(vp="Ana", conf=0.75))[:2], ("high", {"name": "Ana"}))
        self.assertEqual(self.J(ev("Dana", 0.3, 20, vp="Ana", conf=0.9))[0], "high")     # weak captions name nobody
        self.assertEqual(self.J(ev(vp="Ana", conf=0.74))[0], "medium")                   # the voiceprint alone

    def test_disagreement_is_always_low(self):
        c, g, reasons = self.J(ev("Dana", 0.9, 40, vp="Ana", conf=0.95))
        self.assertEqual((c, g), ("low", {"name": "Ana"}))
        self.assertIn("disagree", reasons[-1])
        self.assertEqual(self.J(ev("Dana", 0.45, 40, vp="Ana", conf=0.5))[:2], ("low", {"name": "Dana"}))

    def test_medium_and_low(self):
        self.assertEqual(self.J(ev("Ana", 0.4, 3))[:2], ("medium", {"name": "Ana"}))
        self.assertEqual(self.J(ev("Ana", 0.39, 30))[:2], ("low", {"name": "Ana"}))
        self.assertEqual(self.J(ev())[:2], ("low", None))

    def test_the_only_other_attendee_proposes_and_never_more(self):
        sole = {"name": "Dana Ruiz", "email": "dana@example.com"}
        self.assertEqual(self.J(ev(), sole)[:2], ("medium", {"name": "Dana Ruiz", "email": "dana@example.com"}))
        self.assertEqual(self.J(ev(owner_evidence=True), sole)[:2], ("low", None))      # Alex's own voice
        for e in (ev("Ana", 0.8, 30), ev(vp="Ana", conf=0.9)):
            self.assertEqual(self.J(e, sole)[1], {"name": "Ana"})                       # evidence wins

    def test_learn(self):
        self.assertTrue(speakers.learn_for(20, {"name": "Ana"}, None))
        self.assertFalse(speakers.learn_for(19.9, {"name": "Ana"}, None))
        self.assertFalse(speakers.learn_for(60, {"name": "Ana"}, {"name": "Ana", "confidence": 0.9}))
        self.assertTrue(speakers.learn_for(60, {"name": "Dana"}, {"name": "Ana", "confidence": 0.9}))


# ── a synthetic meeting ─────────────────────────────────────────────────────

START = 1795370400.0          # an exact second; the clocks below are derived in local time


def local_clock(ts):
    d = datetime.fromtimestamp(ts)
    return d.hour * 3600 + d.minute * 60 + d.second


ATTENDEES = [{"email": "ana.lima@example.com"}, {"email": "dana@example.com"}, {"email": "owner@example.com"}]


def meeting_get(started=START, status="needs_review"):
    """Speaker 1 = Ana (captions only), Speaker 2 = Alex (voiceprint + a few captions), Speaker 3 = an
    uninvited guest who introduces himself."""
    return {"meeting": {"id": "m1", "title": "Partner sync", "status": status, "started_at": started},
            "clusters": [
                {"id": "c-ana", "label": "Speaker 1", "speech_seconds": 25.0, "assignment": "suggested",
                 "person_id": None, "confidence": 0.3},
                {"id": "c-ben", "label": "Alex Morgan", "speech_seconds": 20.0, "assignment": "auto",
                 "person_id": "p-ben", "confidence": 0.9},
                {"id": "c-x", "label": "Speaker 3", "speech_seconds": 10.0, "assignment": "suggested",
                 "person_id": None, "confidence": 0.1}],
            "utterances": [
                {"id": 1, "cluster_id": "c-ana", "start": 10.0, "end": 20.0, "text": "Here is the plan " + words(16, "ok")},
                {"id": 2, "cluster_id": "c-ana", "start": 30.0, "end": 45.0, "text": "And the budget " + words(22, "fine")},
                {"id": 3, "cluster_id": "c-ben", "start": 50.0, "end": 70.0, "text": "Thanks Ana " + words(18, "yes")},
                {"id": 4, "cluster_id": "c-x", "start": 80.0, "end": 90.0,
                 "text": "Hi everyone, this is Sam from the co-op, sorry I'm late"}]}


def meeting_captions(started=START):
    lines = [(21, "Ana Lima", "Here is the plan " + words(16, "ok")),
             (46, "Ana Lima", "And the budget " + words(22, "fine")),
             (71, "Alex Morgan", "Thanks Ana " + words(18, "yes")),
             (92, "You", "Hello")]
    return [cap(i, local_clock(started + off), who, text) for i, (off, who, text) in enumerate(lines)]


def transcript(started=START):
    out = []
    for c in meeting_captions(started):
        out.append("[%s] %s: %s" % (c.clock, c.speaker, c.text))
    return "\n".join(out) + "\n"


class Plan(unittest.TestCase):
    def plan(self, segs=None, attendees=ATTENDEES):
        return speakers.plan(segs or [("m1", meeting_get())], meeting_captions(), attendees, OWNER, OWN)

    def test_rows(self):
        pl = self.plan()
        rows = {r["id"]: r for r in pl["rows"]}
        self.assertEqual([r["id"] for r in pl["rows"]], ["c-ana", "c-ben", "c-x"])     # most speech first
        ana, ben, x = rows["c-ana"], rows["c-ben"], rows["c-x"]
        self.assertEqual((ana["kind"], ana["confidence"], ana["guess"]),
                         ("auto", "high", {"name": "Ana Lima", "email": "ana.lima@example.com"}))
        self.assertTrue(ana["learn"])
        self.assertEqual((ben["kind"], ben["guess"]), ("auto", {"name": "Alex Morgan", "email": "owner@example.com"}))
        self.assertFalse(ben["learn"], "already matched by voiceprint to this person")
        self.assertEqual((x["kind"], x["confidence"], x["guess"]), ("ask", "low", None))
        self.assertEqual(x["quote"], "Hi everyone, this is Sam from the co-op, sorry I'm late")
        self.assertEqual(pl["started_at"], START)
        self.assertEqual([c["name"] for c in pl["candidates"]], ["Alex Morgan", "Ana Lima", "Dana"])

    def test_one_other_attendee_proposes_the_unknown_speaker(self):
        pl = self.plan(attendees=[{"email": "ana.lima@example.com"}, {"email": "owner@example.com"}])
        x = {r["id"]: r for r in pl["rows"]}["c-x"]
        self.assertEqual((x["kind"], x["confidence"], x["guess"]["name"]), ("ask", "medium", "Ana Lima"))

    def test_named_in_minutes_already_is_kept(self):
        seg = meeting_get()
        seg["clusters"][2].update(assignment="user", label="Sam Rivers")
        x = {r["id"]: r for r in self.plan([("m1", seg)])["rows"]}["c-x"]
        self.assertEqual((x["kind"], x["preassigned"], x["guess"]), ("auto", True, {"name": "Sam Rivers"}))

    def test_two_parts_and_row_ids(self):
        later = START + 600
        seg2 = meeting_get(later)
        seg2["clusters"] = [dict(seg2["clusters"][0], id="c-ana"), dict(seg2["clusters"][2], id="bad id!")]
        caps = meeting_captions() + meeting_captions(later)
        pl = speakers.plan([("m1", meeting_get()), ("m2", seg2)], caps, ATTENDEES, OWNER, OWN)
        ids = [r["id"] for r in pl["rows"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(speakers.ROW_ID_RE.match(i) for i in ids))
        self.assertEqual({r["mid"] for r in pl["rows"]}, {"m1", "m2"})
        self.assertTrue(all("(part " in r["label"] for r in pl["rows"]))
        self.assertEqual(pl["started_at"], START)

    def test_at_most_forty_rows(self):
        seg = meeting_get()
        seg["clusters"] = [{"id": "k%d" % i, "label": "Speaker %d" % i, "speech_seconds": float(i),
                            "assignment": "suggested"} for i in range(1, 50)]
        rows = speakers.plan([("m1", seg)], [], ATTENDEES, OWNER, OWN)["rows"]
        self.assertEqual(len(rows), speakers.MAX_ROWS)
        self.assertEqual(rows[-1]["seconds"], 10)


# ── step 4: the model's answer ──────────────────────────────────────────────

class Judgement(unittest.TestCase):
    def setUp(self):
        self.pl = speakers.plan([("m1", meeting_get())], meeting_captions(), ATTENDEES, OWNER, OWN)
        self.rows = self.pl["rows"]
        self.c = self.pl["candidates"]

    def apply(self, *entries):
        speakers.apply_judgement(self.rows, self.c, {"speakers": list(entries)})
        return {r["id"]: r for r in self.rows}

    def test_prompt_has_only_the_asked_speakers(self):
        p = speakers.model_prompt(self.rows, self.c, "Alex Morgan")
        self.assertIn("Speaker id: c-x", p)
        self.assertNotIn("Speaker id: c-ana", p)
        self.assertIn("this is Sam", p)
        self.assertIn("ana.lima@example.com", p)

    def test_a_person_named_in_the_lines_is_accepted_without_an_email(self):
        x = self.apply({"cluster_id": "c-x", "guess": {"name": "Sam", "email": "sam@example.com"},
                        "confidence": "medium", "reasons": ["introduces himself as Sam", "b", "c", "d"]})["c-x"]
        self.assertEqual((x["confidence"], x["guess"], len(x["reasons"])), ("medium", {"name": "Sam"}, 3))

    def test_never_invents_never_raises_to_high_never_touches_auto(self):
        r = self.apply({"cluster_id": "c-x", "guess": {"name": "Priya Patel", "email": None}, "confidence": "medium",
                        "reasons": []},
                       {"cluster_id": "c-ana", "guess": {"name": "Dana", "email": None}, "confidence": "low", "reasons": []})
        self.assertEqual((r["c-x"]["confidence"], r["c-x"]["guess"]), ("low", None))
        self.assertEqual(r["c-ana"]["guess"]["name"], "Ana Lima")
        r = self.apply({"cluster_id": "c-x", "guess": {"name": "Dana", "email": None}, "confidence": "high", "reasons": []})
        self.assertEqual(r["c-x"]["confidence"], "low")

    def test_candidates_are_returned_in_their_own_spelling(self):
        r = self.apply({"cluster_id": "c-x", "guess": {"name": "x", "email": "DANA@example.com"}, "confidence": "medium",
                        "reasons": ["thanked by name"]})
        self.assertEqual(r["c-x"]["guess"], {"name": "Dana", "email": "dana@example.com"})
        r = self.apply({"cluster_id": "c-x", "guess": {"name": "ana", "email": None}, "confidence": "low", "reasons": []})
        self.assertEqual(r["c-x"]["guess"]["email"], "ana.lima@example.com")

    def test_failure_keeps_the_deterministic_guess(self):
        before = json.dumps(self.rows, sort_keys=True)
        for bad in (None, {"speak": False}, {"speakers": "x"}, {"speakers": [None, {"cluster_id": "nope"}]}):
            speakers.apply_judgement(self.rows, self.c, bad)
        self.assertEqual(json.dumps(self.rows, sort_keys=True), before)

    def test_schema_matches_the_contract(self):
        item = speakers.MODEL_SCHEMA["properties"]["speakers"]["items"]
        self.assertEqual(set(item["required"]), {"cluster_id", "guess", "confidence", "reasons"})
        self.assertEqual(item["properties"]["confidence"]["enum"], ["medium", "low"])


# ── step 7: the review body ─────────────────────────────────────────────────

class Body(unittest.TestCase):
    def test_shape(self):
        rows = speakers.plan([("m1", meeting_get())], meeting_captions(), ATTENDEES, OWNER, OWN)["rows"]
        clip = {"mime": "audio/mp4", "base64": "AAAA", "durationS": 9.6}
        b = speakers.review_body("k", "Partner sync", START, rows, [{"name": "Ana", "email": "a@example.com", "x": 1}],
                                 {"c-x": clip})
        self.assertEqual(b["status"], "open")
        self.assertEqual(b["heldAt"], datetime.utcfromtimestamp(START).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(b["candidates"], [{"name": "Ana", "email": "a@example.com"}])
        x = [r for r in b["rows"] if r["id"] == "c-x"][0]
        self.assertEqual((x["hasClip"], x["clip"]), (True, clip))
        allowed = {"id", "label", "seconds", "kind", "confidence", "guess", "reasons", "quote", "hasClip", "clip"}
        for r in b["rows"]:
            self.assertLessEqual(set(r), allowed)
            self.assertLessEqual(len(r["reasons"]), 3)
        auto_only = [r for r in rows if r["kind"] == "auto"]
        self.assertEqual(speakers.review_body("k", "t", START, auto_only, [])["status"], "done")

    def test_answers(self):
        row = {"guess": {"name": "Ana Lima", "email": "ana.lima@example.com"}}
        t = speakers.answer_target
        self.assertEqual(t(row, {"choice": "confirm"}), ("assign", row["guess"]))
        self.assertEqual(t({"guess": None}, {"choice": "confirm"}), ("invalid", None))
        self.assertEqual(t(row, {"choice": "other", "name": " Sam  Rivers ", "email": "bad"}), ("assign", {"name": "Sam Rivers"}))
        self.assertEqual(t(row, {"choice": "other", "name": ""}), ("invalid", None))
        self.assertEqual(t(row, {"choice": "skip"}), ("skip", None))
        a = {"choice": "skip", "at": "1"}
        self.assertNotEqual(speakers.answer_signature(a), speakers.answer_signature(dict(a, at="2")))


# ── the Minutes client: clip, assign, export ────────────────────────────────

class MinutesClient(unittest.TestCase):
    def setUp(self):
        self.calls = []
        p = patch.object(minutes, "call", lambda method, params=None, timeout=0: self.calls.append((method, params)) or {})
        p.start()
        self.addCleanup(p.stop)
        self.dir = Path(tempfile.mkdtemp(prefix="minutes-clips-"))
        e = patch.dict(os.environ, {"MINUTES_CLIPS_DIR": str(self.dir)})
        e.start()
        self.addCleanup(e.stop)

    def test_params(self):
        minutes.clip("m1", "c1", 99)
        minutes.clip("m1", "c1", 1)
        minutes.assign("m1", "c1", "Ana", "ana@example.com", learn=True)
        minutes.assign("m1", "c1", "Ana")
        minutes.export("m1")
        self.assertEqual(self.calls, [
            ("clip", {"meeting_id": "m1", "cluster_id": "c1", "max_seconds": 20}),
            ("clip", {"meeting_id": "m1", "cluster_id": "c1", "max_seconds": 3}),
            ("assign", {"meeting_id": "m1", "cluster_id": "c1", "name": "Ana", "learn": True, "email": "ana@example.com"}),
            ("assign", {"meeting_id": "m1", "cluster_id": "c1", "name": "Ana", "learn": False}),
            ("export", {"meeting_id": "m1"})])

    def test_read_clip_only_inside_the_clips_directory(self):
        good = self.dir / "a.m4a"
        good.write_bytes(M4A)
        self.assertEqual(minutes.read_clip({"path": str(good)}), M4A)
        notmp4 = self.dir / "n.m4a"
        notmp4.write_bytes(b"RIFF....WAVE")
        self.assertIsNone(minutes.read_clip({"path": str(notmp4)}), "only an MP4 (ftyp first)")
        outside = Path(tempfile.mkdtemp()) / "b.m4a"
        outside.write_bytes(b"x")
        self.assertIsNone(minutes.read_clip({"path": str(outside)}))
        link = self.dir / "c.m4a"
        os.symlink(outside, link)
        self.assertIsNone(minutes.read_clip({"path": str(link)}), "a symlink out is not followed")
        (self.dir / "d.txt").write_bytes(b"x")
        self.assertIsNone(minutes.read_clip({"path": str(self.dir / "d.txt")}))
        big = self.dir / "e.m4a"
        big.write_bytes(M4A + b"x" * minutes.CLIP_MAX_BYTES)
        self.assertIsNone(minutes.read_clip({"path": str(big)}))
        self.assertIsNone(minutes.read_clip({"path": str(self.dir / "missing.m4a")}))
        self.assertIsNone(minutes.read_clip({}))


# ── the Prism hook client ───────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _answer(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null")
        _Handler.seen.append(("POST", self.path, self.headers.get("Authorization"), body))
        self._answer(201 if self.path.endswith("speaker-reviews") else 200, {"review": {"id": "sr_ab12"}})

    def do_GET(self):
        _Handler.seen.append(("GET", self.path, self.headers.get("Authorization"), None))
        self._answer(200, {"review": {"id": "sr_ab12", "answers": {}}})


class HookClient(unittest.TestCase):
    def setUp(self):
        _Handler.seen = []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.env = {"OMNI_GATEWAY_URL": "http://127.0.0.1:%d" % self.srv.server_address[1],
                    "OMNI_SERVICE_TOKEN": "t" * 24}

    def test_post_get_close(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(hooks.post_review({"meetingKey": "k"}), (201, {"review": {"id": "sr_ab12"}}))
            self.assertEqual(hooks.get_review("sr_ab12")[0], 200)
            self.assertEqual(hooks.close_review("sr_ab12", "done")[0], 200)
            self.assertEqual(hooks.close_review("sr_ab12", "open")[0], 400)           # never sent
            self.assertEqual(hooks.get_review("../etc")[0], 404)                      # never sent
        self.assertEqual([(m, p) for m, p, _, _ in _Handler.seen],
                         [("POST", "/api/omni/hooks/speaker-reviews"), ("GET", "/api/omni/hooks/speaker-reviews/sr_ab12"),
                          ("POST", "/api/omni/hooks/speaker-reviews/sr_ab12/close")])
        self.assertTrue(all(a == "Bearer " + "t" * 24 for _, _, a, _ in _Handler.seen))
        self.assertEqual(_Handler.seen[-1][3], {"status": "done"})

    def test_loopback_and_token_only(self):
        for url in ("https://127.0.0.1:1", "http://example.com:8787", "http://u:p@127.0.0.1:1", "http://127.0.0.1:1/x"):
            with patch.dict(os.environ, dict(self.env, OMNI_GATEWAY_URL=url)):
                self.assertEqual(hooks.post_review({}), (0, None), url)
        with patch.dict(os.environ, dict(self.env, OMNI_SERVICE_TOKEN="short")):
            self.assertEqual(hooks.post_review({}), (0, None))
        self.assertEqual(_Handler.seen, [])

    def test_unreachable_is_zero_and_logs_no_secret(self):
        with patch.dict(os.environ, dict(self.env, OMNI_GATEWAY_URL="http://127.0.0.1:9")), \
                self.assertLogs(hooks.logger, "WARNING") as logs:
            self.assertEqual(hooks.get_review("sr_1"), (0, None))
        self.assertNotIn("t" * 24, "\n".join(logs.output))


# ── the conductor's naming stage ────────────────────────────────────────────

class FakeMinutes:
    """Minutes' get / assign / clip / export, in process."""

    def __init__(self, clips_dir):
        self.calls, self.fail = [], {}
        self.meetings = {"m1": meeting_get(status="processing")}
        self.clip_seconds = 9.6
        self.clips_dir = Path(clips_dir)

    def _maybe_fail(self, method):
        if self.fail.get(method):
            raise self.fail[method].pop(0)

    def get(self, mid):
        self.calls.append(("get", mid))
        self._maybe_fail("get")
        if mid not in self.meetings:
            raise minutes.MinutesError("not_found", "")
        return json.loads(json.dumps(self.meetings[mid]))

    def assign(self, mid, cid, name, email=None, learn=False):
        self.calls.append(("assign", mid, cid, name, email, learn))
        self._maybe_fail("assign")
        return {"cluster": {"id": cid}, "person_id": "p", "learned": learn}

    def clip(self, mid, cid, max_seconds=10):
        self.calls.append(("clip", mid, cid, max_seconds))
        self._maybe_fail("clip")
        if not 3 <= max_seconds <= 20:                                      # like Minutes: not clamped
            raise minutes.MinutesError("bad_request", "max_seconds must be 3-20")
        p = self.clips_dir / ("%s-%s.m4a" % (mid, cid))
        p.write_bytes(M4A + cid.encode())
        return {"path": str(p), "duration_s": self.clip_seconds, "segments": [{"start": 10.0, "end": 19.6, "text": "x"}]}

    def export(self, mid):
        self.calls.append(("export", mid))
        self._maybe_fail("export")
        return {"exported": True, "path": "vault://x", "note_id": "n"}

    def named(self, method):
        return [c for c in self.calls if c[0] == method]


class FakePrism:
    def __init__(self):
        self.posts, self.gets, self.closes, self.answers = [], [], [], {}
        self.keys, self.rows = {}, []
        self.down = False
        self.post_status = 201

    KEY = re.compile(r"^[A-Za-z0-9._:@/+=-]{1,200}$")

    def check(self, body):
        """The server's validation, as far as the contract and the coordinator's notes say."""
        assert self.KEY.match(body["meetingKey"]), body["meetingKey"]
        assert len(body["rows"]) <= 40 and len(body["candidates"]) <= 30
        for r in body["rows"]:
            if r["kind"] == "auto":
                assert r["guess"] and r["confidence"] == "high", r
            else:
                assert r["confidence"] in ("medium", "low"), r
            if "clip" in r:
                raw = base64.b64decode(r["clip"]["base64"])
                assert raw[4:8] == b"ftyp" and len(raw) <= 600 * 1024, r["id"]
                assert 0 < r["clip"]["durationS"] <= 60 and r["clip"]["mime"] == "audio/mp4"
        assert len(json.dumps(body)) <= 33 * 1024 * 1024

    def post(self, body):
        if self.down:
            return 0, None
        self.check(body)
        self.posts.append(body)
        if self.post_status != 201:
            return self.post_status, {"error": "refused"}
        existing = self.keys.get(body["meetingKey"])
        if existing and self.answers:
            return 409, {"error": "answered"}
        rid = existing or "sr_%04d" % (len(self.keys) + 1)
        self.keys[body["meetingKey"]] = rid
        self.rows = body["rows"]
        return (200 if existing else 201), {"review": self.review(rid, body.get("status"))}

    def review(self, rid, status="open"):
        asks = [r["id"] for r in self.rows if r["kind"] == "ask"]
        return {"id": rid, "meetingKey": next((k for k, v in self.keys.items() if v == rid), ""),
                "status": status, "updatedAt": "2026-11-22T00:00:00Z", "rows": self.rows,
                "answers": dict(self.answers), "pending": len([a for a in asks if a not in self.answers])}

    def get(self, rid):
        self.gets.append(rid)
        if self.down:
            return 0, None
        return 200, {"review": self.review(rid)}

    def close(self, rid, status):
        if self.down:
            return 0, None
        self.closes.append((rid, status))
        return 200, {"review": {"id": rid, "status": status}}


class Naming(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="omni-meet-home-")
        clips = Path(self.home) / "clips"
        clips.mkdir()
        p = patch.dict(os.environ, {"HERMES_HOME": self.home, "MINUTES_CLIPS_DIR": str(clips),
                                    "MINUTES_CONTROL_SOCKET": os.path.join(self.home, "no-minutes.sock"),
                                    "OMNI_MEET_OWNER_NAMES": "Alex Morgan,Alex"})
        p.start()
        self.addCleanup(p.stop)
        for k in ("OMNI_MEET_SPEAKERS", "OMNI_MEET_MINUTES"):   # restored by patch.dict
            os.environ.pop(k, None)
        self.mm = FakeMinutes(clips)
        self.prism = FakePrism()
        self.sent, self.prompts = [], []
        self.model = {"speakers": [{"cluster_id": "c-x", "guess": {"name": "Sam", "email": None},
                                    "confidence": "medium", "reasons": ["introduces himself as Sam"]}]}
        out = Path(tempfile.mkdtemp())
        (out / "transcript.txt").write_text(transcript())
        for target, name, value in (
                (minutes, "get", self.mm.get), (minutes, "assign", self.mm.assign), (minutes, "clip", self.mm.clip),
                (minutes, "export", self.mm.export),
                (minutes, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("real socket call"))),
                (hooks, "post_review", self.prism.post), (hooks, "get_review", self.prism.get),
                (hooks, "close_review", self.prism.close),
                (hooks, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("real hook call"))),
                (gcal, "OWNER_EMAIL", "owner@example.com"),
                (conductor, "notify", self.sent.append),
                (conductor, "ask_model", lambda prompt, system=None, schema=None: (self.prompts.append(prompt), self.model)[1])):
            q = patch.object(target, name, value)
            q.start()
            self.addCleanup(q.stop)
        self.t = START + 4000                     # the meeting ended; Minutes stopped at t - 10
        store.update("ev1", lambda r: {"key": "ev1", "title": "Partner sync", "status": "done", "start": START,
                                       "end": START + 1800, "out_dir": str(out), "recording": False,
                                       "minutes_meeting_ids": ["m1"], "recording_stopped_at": self.t - 10,
                                       "attendees": [a["email"] for a in ATTENDEES]})
        self.c = self.conductor()

    def conductor(self):
        c = conductor.Conductor()
        c.naming_async = False
        return c

    def tick(self, dt=0.0):
        self.t += dt
        self.c._naming(self.t)

    def n(self):
        return store.load()["ev1"].get("naming") or {}

    def ready(self):
        self.mm.meetings["m1"]["meeting"]["status"] = "needs_review"

    def posted(self):
        self.tick()
        self.ready()
        self.tick(30)
        self.assertEqual(self.n()["stage"], "review")

    def test_waits_for_processing_polling_at_most_every_30_s(self):
        for _ in range(20):                                                 # one minute of 3 s ticks
            self.tick(3)
        self.assertEqual(len(self.mm.named("get")), 2)
        self.assertEqual(self.n()["stage"], "waiting")
        self.ready()
        self.tick(30)
        self.assertEqual(self.n()["stage"], "review")

    def test_plan_auto_names_clips_and_posts(self):
        self.posted()
        self.assertEqual(self.mm.named("assign"), [
            ("assign", "m1", "c-ana", "Ana Lima", "ana.lima@example.com", True),
            ("assign", "m1", "c-ben", "Alex Morgan", "owner@example.com", False)])
        self.assertEqual(self.mm.named("clip"), [("clip", "m1", "c-x", 10)])
        self.assertEqual(self.mm.named("export"), [], "not before the answers")
        body = self.prism.posts[0]
        self.assertEqual((body["meetingKey"], body["title"], body["status"]), ("omni-meet:ev1:m1", "Partner sync", "open"))
        rows = {r["id"]: r for r in body["rows"]}
        self.assertEqual((rows["c-ana"]["kind"], rows["c-ben"]["kind"], rows["c-x"]["kind"]), ("auto", "auto", "ask"))
        self.assertEqual(rows["c-x"]["guess"], {"name": "Sam"})                       # the model's, validated
        self.assertEqual(rows["c-x"]["clip"]["mime"], "audio/mp4")
        self.assertTrue(rows["c-x"]["hasClip"])
        self.assertNotIn("clip", rows["c-ana"])
        self.assertEqual(len(self.prompts), 1)
        self.assertNotIn("Speaker id: c-ana", self.prompts[0])
        n = self.n()
        self.assertEqual((n["review_id"], n["posted_at"]), ("sr_0001", self.t))
        self.assertFalse(any("texts" in r or "evidence" in r for r in n["rows"]), "no transcript in the store")

    def test_restart_repeats_nothing(self):
        self.posted()
        before = list(self.mm.calls)
        self.c = self.conductor()
        for _ in range(5):
            self.tick(30)
        self.assertEqual(len(self.prism.posts), 1)
        self.assertEqual([c for c in self.mm.calls if c not in before and c[0] != "get"], [])

    def test_a_crash_between_auto_names_resumes_where_it_stopped(self):
        self.tick()
        self.ready()
        calls = []

        def assign_then_crash(*a, **k):
            calls.append(a)
            if len(calls) == 2:
                raise KeyboardInterrupt("gateway restarted")
            return self.mm.assign(*a, **k)
        with patch.object(minutes, "assign", assign_then_crash):
            self.t += 30
            with self.assertRaises(KeyboardInterrupt):
                self.c._naming(self.t)
        self.assertEqual((self.n()["stage"], self.n()["auto_done"]), ("planned", ["c-ana"]))
        self.mm.fail = {}
        self.c = self.conductor()
        self.tick(1)
        self.assertEqual([c[2] for c in self.mm.named("assign")], ["c-ana", "c-ben"])
        self.assertEqual(len(self.prism.posts), 1)

    def test_answers_are_applied_once_then_export_and_close(self):
        self.posted()
        self.prism.answers = {"c-x": {"choice": "other", "name": "Sam Rivers", "email": "sam@example.com", "at": "t1"}}
        self.tick(30)
        self.tick(30)
        sam = [c for c in self.mm.named("assign") if c[2] == "c-x"]
        self.assertEqual(sam, [("assign", "m1", "c-x", "Sam Rivers", "sam@example.com", False)])   # 10 s: no learn
        self.assertEqual(self.mm.named("export"), [("export", "m1")])
        self.assertEqual(self.prism.closes, [("sr_0001", "done")])
        # A correction later (the owner re-answers, or says an automatic name is wrong): applied once more.
        self.prism.answers["c-ana"] = {"choice": "other", "name": "Ana Souza", "email": None, "at": "t2"}
        self.prism.answers["c-x"] = {"choice": "confirm", "name": None, "email": None, "at": "t3"}
        self.tick(conductor.NAMING_IDLE_POLL_SECONDS)
        self.tick(conductor.NAMING_IDLE_POLL_SECONDS)
        tail = self.mm.named("assign")[-2:]
        self.assertEqual(sorted(c[2:5] for c in tail), [("c-ana", "Ana Souza", None), ("c-x", "Sam", None)])
        self.assertEqual(tail[0][5], True, "25 s of speech: learn")
        self.assertEqual(len(self.mm.named("export")), 1)
        self.assertEqual(len(self.prism.closes), 1)

    def test_closed_reviews_are_polled_less_and_finish_after_seven_days(self):
        self.posted()
        self.prism.answers = {"c-x": {"choice": "skip", "at": "t1"}}
        self.tick(30)
        self.assertEqual(self.n()["closed"], "done")
        gets = len(self.prism.gets)
        for _ in range(10):
            self.tick(30)
        self.assertEqual(len(self.prism.gets) - gets, 1)
        self.assertEqual([c for c in self.mm.named("assign") if c[2] == "c-x"], [], "skip assigns nothing")
        self.tick(conductor.REVIEW_CORRECTION_SECONDS)
        self.assertEqual(self.n()["stage"], "finished")
        gets = len(self.prism.gets)
        self.tick(conductor.NAMING_IDLE_POLL_SECONDS)
        self.assertEqual(len(self.prism.gets), gets)

    def test_unanswered_for_24_h_exports_and_expires(self):
        self.posted()
        self.tick(conductor.REVIEW_ANSWER_SECONDS - 31)
        self.assertEqual(self.mm.named("export"), [])
        self.tick(31)
        self.assertEqual(self.mm.named("export"), [("export", "m1")])
        self.assertEqual(self.prism.closes, [("sr_0001", "expired")])

    def test_prism_down_at_24_h_still_exports_and_closes_later(self):
        self.posted()
        self.prism.down = True
        self.tick(conductor.REVIEW_ANSWER_SECONDS)
        self.assertEqual(self.mm.named("export"), [("export", "m1")])
        self.assertEqual(self.prism.closes, [])
        self.prism.down = False
        self.tick(30)
        self.assertEqual(self.prism.closes, [("sr_0001", "expired")])

    def test_prism_down_for_an_hour_while_waiting_for_answers_tells_once(self):
        self.posted()
        self.prism.down = True
        for _ in range(240):
            self.tick(30)
        self.assertEqual(len([t for t in self.sent if "keeps trying" in t]), 1)
        self.assertEqual(self.n()["stage"], "review")

    def test_everyone_named_automatically_exports_at_once_and_posts_done(self):
        self.mm.meetings["m1"]["clusters"] = self.mm.meetings["m1"]["clusters"][:2]
        self.posted()
        self.assertEqual(self.mm.named("export"), [("export", "m1")])
        self.assertEqual(self.prism.posts[0]["status"], "done")
        self.assertEqual(self.prompts, [], "no model call without ask rows")
        self.assertEqual(self.n()["closed"], "done")
        self.prism.answers = {"c-ana": {"choice": "other", "name": "Ana Souza", "at": "t9"}}
        self.tick(conductor.NAMING_IDLE_POLL_SECONDS)
        self.assertEqual(self.mm.named("assign")[-1][2:4], ("c-ana", "Ana Souza"))
        self.assertEqual(self.prism.closes, [], "already done")

    def test_learn_refused_is_retried_without_learning(self):
        self.mm.fail["assign"] = [minutes.MinutesError("bad_request", "cannot learn")]
        self.posted()
        self.assertEqual([c[2:] for c in self.mm.named("assign")][:3], [
            ("c-ana", "Ana Lima", "ana.lima@example.com", True), ("c-ana", "Ana Lima", "ana.lima@example.com", False),
            ("c-ben", "Alex Morgan", "owner@example.com", False)])

    def test_busy_answers_wait_for_a_later_tick(self):
        self.posted()
        self.prism.answers = {"c-x": {"choice": "confirm", "at": "t1"}}
        self.mm.fail["assign"] = [minutes.MinutesError("busy", "")]
        self.tick(30)
        self.assertNotIn("c-x", self.n()["applied"])
        self.assertEqual(self.mm.named("export"), [], "no export before every answer is in Minutes")
        self.tick(30)
        self.assertIn("c-x", self.n()["applied"])
        self.assertEqual(len([c for c in self.mm.named("assign") if c[2] == "c-x"]), 2)
        self.assertEqual(self.prism.closes, [("sr_0001", "done")])

    def test_minutes_unreachable_gives_up_after_an_hour_with_one_line(self):
        self.mm.fail["get"] = [minutes.MinutesError("unavailable", "")] * 500
        for _ in range(130):
            self.tick(30)
        self.assertEqual(self.n()["stage"], "failed")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("could not name the speakers of Partner sync", self.sent[0])
        self.assertLessEqual(len(self.mm.named("get")), 125)

    def test_recording_gone_gives_up_too(self):
        self.mm.meetings = {}
        for _ in range(125):
            self.tick(30)
        self.assertEqual(self.n()["stage"], "failed")
        self.assertIn("no longer has its recording", self.sent[-1])

    def test_prism_unreachable_retries_then_exports_and_tells_once(self):
        self.prism.down = True
        self.tick()
        self.ready()
        for _ in range(125):
            self.tick(30)
        self.assertEqual(self.n()["stage"], "failed")
        self.assertEqual(len(self.mm.named("assign")), 2, "auto names once, despite the retries")
        self.assertEqual(self.mm.named("export"), [("export", "m1")])
        self.assertEqual(len(self.sent), 1)

    def test_a_refused_review_is_not_retried(self):
        self.prism.post_status = 400
        self.tick()
        self.ready()
        self.tick(30)
        self.tick(30)
        self.assertEqual((self.n()["stage"], len(self.prism.posts)), ("failed", 1))
        self.assertIn("refused", self.sent[-1])

    def test_a_clip_with_no_usable_duration_is_left_out(self):
        self.mm.clip_seconds = 0
        self.posted()
        x = [r for r in self.prism.posts[0]["rows"] if r["id"] == "c-x"][0]
        self.assertEqual((x["hasClip"], "clip" in x), (False, False))

    def test_a_repost_after_a_lost_answer_keeps_the_review(self):
        self.prism.down = True                        # the first POST reached Prism but its answer was lost
        self.tick()
        self.ready()
        self.prism.down = False
        real = self.prism.post
        with patch.object(hooks, "post_review", lambda b: (real(b), (0, None))[1]):
            self.tick(30)
        self.assertEqual(self.n()["stage"], "planned")
        self.tick(30)                                   # re-POST: same key, no answers -> 200, same id
        self.assertEqual((self.n()["stage"], self.n()["review_id"]), ("review", "sr_0001"))
        self.assertEqual(len(self.prism.posts), 2)
        self.assertEqual(len(self.mm.named("assign")), 2, "auto names once")

    def test_model_failure_keeps_the_deterministic_guess(self):
        self.model = {"speak": False, "text": "", "reason": "model call failed"}
        self.posted()
        x = [r for r in self.prism.posts[0]["rows"] if r["id"] == "c-x"][0]
        self.assertEqual((x["confidence"], x["guess"]), ("low", None))

    def test_not_while_still_in_the_meeting_or_long_ago(self):
        store.update("ev1", lambda r: dict(r, status="joined"))
        self.tick(30)
        self.assertEqual((self.mm.calls, self.n()), ([], {}))
        store.update("ev1", lambda r: dict(r, status="done", recording_stopped_at=self.t - conductor.NAMING_START_WINDOW_SECONDS - 1))
        self.tick(30)
        self.assertEqual(self.mm.calls, [])

    def test_a_later_recording_under_the_same_key_is_named_on_its_own(self):
        self.mm.meetings["m1"]["clusters"] = self.mm.meetings["m1"]["clusters"][:2]
        self.posted()
        self.tick(conductor.REVIEW_CORRECTION_SECONDS + 1)
        self.assertEqual(self.n()["stage"], "finished")
        self.mm.meetings["m2"] = dict(meeting_get(), meeting=dict(meeting_get()["meeting"], id="m2"))
        store.update("ev1", lambda r: dict(r, minutes_meeting_ids=["m1", "m2"], recording_stopped_at=self.t))
        self.tick(1)
        self.assertEqual((self.n()["mids"], self.n()["done_mids"]), (["m2"], ["m1"]))
        self.assertEqual(self.n()["stage"], "review")
        self.assertEqual(self.prism.posts[-1]["meetingKey"], "omni-meet:ev1:m2")

    def test_switched_off_does_nothing(self):
        for env in ({"OMNI_MEET_SPEAKERS": "off"}, {"OMNI_MEET_MINUTES": "off"}):
            with patch.dict(os.environ, env):
                self.ready()
                for _ in range(5):
                    self.tick(30)
            self.assertEqual((self.mm.calls, self.prism.posts, self.prism.gets, self.n()), ([], [], [], {}), env)

    def test_the_tick_runs_naming_and_survives_its_failure(self):
        with patch.object(conductor.meetbot, "status", lambda: {"ok": False}), \
                patch.object(minutes, "status", lambda: {"processing": False}):
            self.ready()
            self.c.tick(self.t)
            self.assertEqual(self.n()["stage"], "review")
            with patch.object(self.c, "_naming_poll", side_effect=RuntimeError("boom")), \
                    self.assertLogs(conductor.logger, "ERROR"):
                self.c.tick(self.t + 30)


class CalendarNames(unittest.TestCase):
    def test_display_names_are_kept_and_passed_to_minutes(self):
        ev = gcal.parse_event({"id": "e", "attendees": [{"email": "Ana.Lima@example.com", "displayName": " Ana Lima "},
                                                        {"email": "dana@example.com"}]})
        self.assertEqual(ev["attendee_names"], {"ana.lima@example.com": "Ana Lima"})
        m = {"attendees": ev["attendees"], "attendee_names": ev["attendee_names"]}
        self.assertEqual(conductor._attendees(m), [{"email": "ana.lima@example.com", "name": "Ana Lima"},
                                                   {"email": "dana@example.com"}])


class PrismRules(unittest.TestCase):
    def test_meeting_key_characters_and_length(self):
        k = speakers.meeting_key("omni-meet", "abc_20261122T170000Z", "0F1E-uuid")
        self.assertEqual(k, "omni-meet:abc_20261122T170000Z:0F1E-uuid")
        self.assertEqual(speakers.meeting_key("a b", "c#d"), "a-b:c-d")
        self.assertTrue(FakePrism.KEY.match(speakers.meeting_key("x" * 300, "é ü/?")))
        self.assertEqual(len(speakers.meeting_key("x" * 300)), 200)

    def test_a_high_cluster_without_a_name_is_asked(self):
        with patch.object(speakers, "judge", lambda ev, sole=None: ("high", None, ["?"])):
            rows = speakers.plan([("m1", meeting_get())], [], ATTENDEES, OWNER, OWN)["rows"]
        self.assertTrue(all(r["kind"] == "ask" and r["confidence"] == "low" for r in rows if not r["preassigned"]))

    def test_pending_count_decides_completeness(self):
        rows = [{"id": "a", "kind": "ask"}, {"id": "b", "kind": "auto"}]
        self.assertTrue(speakers.review_complete({"pending": 0, "answers": {}}, rows))
        self.assertFalse(speakers.review_complete({"pending": 1, "answers": {"a": {}}}, rows))
        self.assertTrue(speakers.review_complete({"answers": {"a": {}}}, rows))      # no count: the answers


class Switch(unittest.TestCase):
    def test_enabled(self):
        self.assertTrue(conductor.speakers_enabled({}))
        self.assertFalse(conductor.speakers_enabled({"OMNI_MEET_SPEAKERS": "off"}))
        self.assertFalse(conductor.speakers_enabled({"OMNI_MEET_SPEAKERS": "0"}))
        self.assertFalse(conductor.speakers_enabled({"OMNI_MEET_MINUTES": "off"}))


if __name__ == "__main__":
    unittest.main()
