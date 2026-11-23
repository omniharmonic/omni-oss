"""Offline tests for the real hold engine. _gog is patched; no calendar is ever touched."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import manage_holds as mh

SLOTS = [
    {"start": "2026-10-27T10:00:00-06:00", "end": "2026-10-27T10:30:00-06:00"},
    {"start": "2026-10-28T14:00:00-06:00", "end": "2026-10-28T14:30:00-06:00"},
    {"start": "2026-10-29T09:00:00-06:00", "end": "2026-10-29T09:30:00-06:00"},
]


class FakeCalendar:
    def __init__(self, fail_create_at=None):
        self.calls, self.events, self.n = [], {}, 0
        self.fail_create_at = fail_create_at
        self.gone = set()
        self.blank_create = False      # create succeeds but the reply has no id
        self.timeout_create = False    # create succeeds server-side but the call raises
        self.lookup_fails = False
        self.next_page = False
        self.lookups = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["calendar", "create"]:
            if self.fail_create_at == len([c for c in self.calls if c[:2] == ["calendar", "create"]]) - 1:
                raise mh.GogError("boom")
            self.n += 1
            eid = f"ev{self.n}"
            self.events[eid] = {"args": list(args)}
            if self.timeout_create:
                raise mh.GogError("timed out")
            if self.blank_create:
                return {}
            return {"event": {"id": eid}}
        if args[:2] == ["calendar", "events"]:
            if self.lookup_fails:
                raise mh.GogError("auth")
            want = opt(args, "--private-prop-filter")
            self.lookups.append(list(args))
            out = []
            for eid, e in self.events.items():
                a = e["args"]
                props = dict(x.split("=", 1) for i, x in enumerate(a) if a[i - 1] == "--private-prop")
                if want and want != "omni_proposal=" + props.get("omni_proposal", ""):
                    continue
                row = {"id": eid, "start": {"dateTime": opt(a, "--from")}, "end": {"dateTime": opt(a, "--to")},
                       "extendedProperties": {"private": props}}
                if e.get("attendees"):
                    row["attendees"] = list(e["attendees"])
                out.append(row)
            if self.next_page:
                return {"events": out, "nextPageToken": "tok"}
            return {"events": out}
        if args[:2] == ["calendar", "delete"]:
            eid = args[3]
            if eid in self.gone or eid not in self.events:
                raise mh.GogNotFound("404 not found")
            del self.events[eid]
            return {}
        if args[:2] == ["calendar", "update"]:
            eid = args[3]
            if eid not in self.events:
                raise mh.GogNotFound("404 not found")
            self.events[eid]["update"] = list(args)
            return {}
        raise AssertionError(args)

    def of(self, verb):
        return [c for c in self.calls if c[:2] == ["calendar", verb]]


def opt(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = Path(self.tmp.name) / "scheduling" / "proposals.json"
        self.p1 = patch.object(mh, "REGISTRY", self.reg)
        self.p1.start()
        self.cal = FakeCalendar()
        self.p2 = patch.object(mh, "_gog", self.cal)
        self.p2.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop(); self.tmp.cleanup()

    def age(self, pid, seconds=3600):
        from datetime import datetime, timedelta, timezone
        data = json.loads(self.reg.read_text())
        data[pid]["created"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        self.reg.write_text(json.dumps(data))

    def place(self, **kw):
        base = dict(email="jane@example.com", thread_ref={"kind": "email", "threadId": "t1"}, card_id="c1")
        base.update(kw)
        return mh.place("Jane Doe", "Grant sync", SLOTS, **base)

    def test_place_creates_private_holds(self):
        prop = self.place()
        self.assertRegex(prop["id"], r"^prop_[0-9a-f]{12}$")
        self.assertEqual(prop["status"], "open")
        self.assertEqual(len(prop["slots"]), 3)
        creates = self.cal.of("create")
        self.assertEqual(len(creates), 3)
        for c, slot in zip(creates, prop["slots"]):
            self.assertEqual(opt(c, "--summary"), "HOLD: Jane Doe — Grant sync")
            self.assertNotIn("--attendees", c)
            self.assertEqual(opt(c, "--transparency"), "opaque")
            props = [c[i + 1] for i, a in enumerate(c) if a == "--private-prop"]
            self.assertIn(f"omni_proposal={prop['id']}", props)
            self.assertIn("omni_hold=1", props)
            self.assertEqual(opt(c, "--account"), "owner@example.com")
            self.assertEqual(opt(c, "--from"), slot["start"])
            self.assertTrue(slot["event_id"].startswith("ev"))

    def test_no_topic_title(self):
        mh.place("Jane Doe", None, SLOTS[:1], email=None, thread_ref=None, card_id=None)
        self.assertEqual(opt(self.cal.of("create")[0], "--summary"), "HOLD: Jane Doe")

    def test_registry_mode_and_content(self):
        prop = self.place()
        self.assertEqual(os.stat(self.reg).st_mode & 0o777, 0o600)
        self.assertEqual(mh.get(prop["id"])["card_id"], "c1")
        self.assertEqual([p["id"] for p in mh.open_proposals()], [prop["id"]])

    def test_confirm_keeps_one_retitles(self):
        prop = self.place()
        out = mh.confirm(prop["id"], 1)
        self.assertEqual(out["status"], "confirmed")
        self.assertEqual(out["confirmed_slot"]["event_id"], prop["slots"][1]["event_id"])
        deleted = {c[3] for c in self.cal.of("delete")}
        self.assertEqual(deleted, {prop["slots"][0]["event_id"], prop["slots"][2]["event_id"]})
        up = self.cal.of("update")[0]
        self.assertEqual(up[3], prop["slots"][1]["event_id"])
        self.assertEqual(opt(up, "--summary"), "Jane Doe / Alex — Grant sync")
        self.assertIn("omni_hold=0", up)
        self.assertEqual(mh.open_proposals(), [])

    def test_confirm_title_override_and_noop_repeat(self):
        prop = self.place()
        first = mh.confirm(prop["id"], 0, title="Custom")
        n = len(self.cal.calls)
        again = mh.confirm(prop["id"], 0)
        self.assertEqual(len(self.cal.calls), n)
        self.assertEqual(first["confirmed_slot"], again["confirmed_slot"])
        self.assertEqual(opt(self.cal.of("update")[0], "--summary"), "Custom")

    def test_release_tolerates_missing_event(self):
        prop = self.place()
        del self.cal.events[prop["slots"][1]["event_id"]]  # Alex deleted it by hand
        out = mh.release(prop["id"], reason="card-dismissed")
        self.assertEqual(out["status"], "released")
        self.assertEqual(out["reason"], "card-dismissed")
        self.assertEqual(self.cal.events, {})
        # second release is a no-op
        n = len(self.cal.calls)
        mh.release(prop["id"], reason="again")
        self.assertEqual(len(self.cal.calls), n)

    def test_confirm_when_kept_hold_missing_is_flagged_not_recreated(self):
        prop = self.place()
        del self.cal.events[prop["slots"][0]["event_id"]]
        out = mh.confirm(prop["id"], 0)
        self.assertTrue(out["confirmed_slot"].get("event_missing"))
        self.assertEqual(len(self.cal.of("create")), 3)

    def test_rollback_on_second_create_failure(self):
        self.cal.fail_create_at = 1
        with self.assertRaises(mh.GogError):
            self.place()
        self.assertEqual(self.cal.events, {})
        self.assertEqual(len(self.cal.of("delete")), 1)
        self.assertEqual(mh.open_proposals(), [])

    def test_rollback_delete_failure_is_recorded_for_sweep(self):
        self.cal.fail_create_at = 1
        real = self.cal.__call__
        def flaky(args):
            if args[:2] == ["calendar", "delete"]:
                raise mh.GogError("network")
            return real(args)
        with patch.object(mh, "_gog", flaky):
            with self.assertRaises(mh.GogError):
                self.place()
        left = mh.list_proposals(status="rollback-pending")
        self.assertEqual(len(left), 1)
        self.assertEqual(len(left[0]["slots"]), 1)
        # release later cleans up
        mh.release(left[0]["id"], reason="sweep")
        self.assertEqual(self.cal.events, {})

    def test_release_keep_index(self):
        prop = self.place()
        out = mh.release(prop["id"], keep_index=2, reason="chosen")
        self.assertEqual(out["status"], "confirmed")
        self.assertEqual(list(self.cal.events), [prop["slots"][2]["event_id"]])

    def test_attach_card(self):
        prop = self.place(card_id=None)
        mh.attach_card(prop["id"], "card9")
        self.assertEqual(mh.get(prop["id"])["card_id"], "card9")

    def test_bad_slot_rejected_before_any_call(self):
        with self.assertRaises(ValueError):
            mh.place("J", None, [{"start": "2026-10-27T11:00:00-06:00", "end": "2026-10-27T10:00:00-06:00"}],
                     email=None, thread_ref=None, card_id=None)
        self.assertEqual(self.cal.calls, [])

    # ---- fix round 1 ----
    def test_not_found_only_for_google_404_410(self):
        import types
        # call the real _gog, bypassing the class-level patch
        real = self.p2
        real.stop()
        try:
            for msg, exc in [("Google API error (410 deleted): Resource has been deleted", mh.GogNotFound),
                             ("Google API error (404 notFound): Not Found", mh.GogNotFound),
                             ("Google API error (401 authError): invalid credentials", mh.GogError),
                             ("calendar not found for account", mh.GogError),
                             ("account gone 404 stale token", mh.GogError)]:
                r = types.SimpleNamespace(returncode=1, stdout="", stderr=msg)
                with patch.object(mh.subprocess, "run", return_value=r):
                    with self.assertRaises(mh.GogError) as cm:
                        mh._gog(["calendar", "delete", "primary", "x"])
                    self.assertIs(type(cm.exception), exc, msg)
        finally:
            real.start()

    def test_auth_error_on_delete_keeps_slot_live(self):
        prop = self.place()
        real = self.cal.__call__
        def auth(args):
            if args[:2] == ["calendar", "delete"]:
                raise mh.GogError("Google API error (401 authError)")
            return real(args)
        with patch.object(mh, "_gog", auth):
            with self.assertRaises(mh.GogError):
                mh.release(prop["id"], reason="x")
        got = mh.get(prop["id"])
        self.assertEqual(got["status"], "open")
        self.assertFalse(any(s.get("deleted") for s in got["slots"]))

    def test_release_keep_index_validated_before_side_effects(self):
        prop = self.place()
        for bad in (-1, 3, 99):
            with self.assertRaises(IndexError):
                mh.release(prop["id"], keep_index=bad, reason="x")
        self.assertEqual(self.cal.of("delete"), [])
        self.assertEqual(mh.get(prop["id"])["status"], "open")
        self.assertEqual(len(self.cal.events), 3)

    def test_corrupt_registry_fails_closed(self):
        prop = self.place()
        self.reg.write_text("{not json")
        for fn in (mh.open_proposals, lambda: self.place(), lambda: mh.release(prop["id"], reason="x")):
            with self.assertRaises(mh.RegistryCorrupt):
                fn()
        self.assertEqual(self.reg.read_text(), "{not json")
        self.assertEqual(len(self.cal.of("create")), 3)  # the failed place made no calendar calls

    def test_backup_of_previous_good_file(self):
        prop = self.place()
        mh.attach_card(prop["id"], "newcard")
        bak = self.reg.with_name(self.reg.name + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(os.stat(bak).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(bak.read_text())[prop["id"]]["status"], "open")

    def test_create_with_unparseable_reply_found_by_props(self):
        self.cal.blank_create = True
        prop = self.place()
        self.assertEqual(prop["status"], "open")
        self.assertEqual([s["event_id"] for s in prop["slots"]], ["ev1", "ev2", "ev3"])

    def test_create_timeout_found_by_props(self):
        self.cal.timeout_create = True
        prop = self.place()
        self.assertEqual(len(prop["slots"]), 3)

    def test_ambiguous_create_and_failed_lookup_leaves_placing_record(self):
        self.cal.timeout_create = True
        self.cal.lookup_fails = True
        with self.assertRaises(mh.GogError):
            self.place()
        recs = mh.list_proposals()
        self.assertEqual(len(recs), 1)
        self.assertIn(recs[0]["status"], ("placing", "rollback-pending"))
        self.assertEqual(len(self.cal.events), 1)  # the orphan exists; reconcile can find it
        self.cal.lookup_fails = False
        self.assertEqual(mh.reconcile(recs[0]["id"])["result"], "reconciled")
        self.assertEqual(len(mh.get(recs[0]["id"])["slots"]), 1)
        mh.release(recs[0]["id"], reason="sweep")
        self.assertEqual(self.cal.events, {})

    def test_save_failure_after_creates_leaves_placing_and_release_cleans(self):
        real_save = mh._save
        calls = {"n": 0}
        def flaky(data):
            calls["n"] += 1
            if calls["n"] >= 3:   # write-ahead + first slot succeed, then the disk goes away
                raise OSError("disk full")
            real_save(data)
        real_gog = self.cal.__call__
        def no_delete(args):  # rollback cannot delete either
            if args[:2] == ["calendar", "delete"]:
                raise mh.GogError("network down")
            return real_gog(args)
        with patch.object(mh, "_save", flaky), patch.object(mh, "_gog", no_delete):
            with self.assertRaises(OSError):
                self.place()
        self.assertEqual(len(self.cal.events), 2)  # second create succeeded but could not be recorded
        rec = mh.list_proposals()[0]
        self.assertEqual(rec["status"], "placing")
        self.assertEqual(len(rec["slots"]), 1)
        self.age(rec["id"])
        self.assertEqual(mh.reconcile(rec["id"])["result"], "reconciled")
        self.assertEqual(len(mh.get(rec["id"])["slots"]), 2)
        self.assertEqual(mh.get(rec["id"])["status"], "rollback-pending")
        mh.release(rec["id"], reason="sweep")
        self.assertEqual(self.cal.events, {})
        self.assertEqual(mh.get(rec["id"])["status"], "released")

    def test_registry_written_before_first_create(self):
        seen = []
        real = self.cal.__call__
        def spy(args):
            if args[:2] == ["calendar", "create"]:
                seen.append([p["status"] for p in mh.list_proposals()])
            return real(args)
        with patch.object(mh, "_gog", spy):
            self.place()
        self.assertEqual(seen[0], ["placing"])

    # ---- fix round 2 ----
    def test_lookup_uses_prop_filter_all_pages_whole_range(self):
        self.cal.blank_create = True
        self.place()
        q = self.cal.lookups[0]
        self.assertIn("--all-pages", q)
        self.assertTrue(q[q.index("--private-prop-filter") + 1].startswith("omni_proposal=prop_"))
        self.assertEqual(opt(q, "--from"), SLOTS[0]["start"])
        self.assertEqual(opt(q, "--to"), SLOTS[2]["end"])

    def test_next_page_token_is_inconclusive_not_not_found(self):
        self.cal.timeout_create = True
        self.cal.next_page = True
        with self.assertRaises(mh.GogError):
            self.place()
        rec = mh.list_proposals()[0]
        self.assertEqual(rec["status"], "rollback-pending")  # kept, never deleted from the registry
        self.cal.next_page = False
        self.assertEqual(mh.reconcile(rec["id"])["result"], "reconciled")
        mh.release(rec["id"], reason="sweep")
        self.assertEqual(self.cal.events, {})

    def test_release_with_inconclusive_lookup_stays_rollback_pending(self):
        self.cal.timeout_create = True
        self.cal.lookup_fails = True
        with self.assertRaises(mh.GogError):
            self.place()
        rec = mh.list_proposals()[0]
        out = mh.release(rec["id"], reason="sweep")
        self.assertEqual(out["status"], "rollback-pending")
        self.assertEqual(len(self.cal.events), 1)  # orphan still there, record kept
        self.cal.lookup_fails = False
        self.assertEqual(mh.release(rec["id"], reason="sweep2")["status"], "released")
        self.assertEqual(self.cal.events, {})

    def test_reconcile_skips_young_placing_record(self):
        self.cal.timeout_create = True
        real_save = mh._save
        n = {"c": 0}
        def flaky(d):
            n["c"] += 1
            if n["c"] >= 2:
                raise OSError("disk")
            real_save(d)
        with patch.object(mh, "_save", flaky), patch.object(mh, "_rollback", lambda *a: None):
            with self.assertRaises(Exception):
                self.place()
        rec = mh.list_proposals()[0]
        self.assertEqual(rec["status"], "placing")
        out = mh.reconcile(rec["id"])
        self.assertEqual(out["result"], "skipped-young")
        self.assertEqual(mh.get(rec["id"])["status"], "placing")
        with self.assertRaises(mh.PlacingInProgress):
            mh.release(rec["id"], reason="x")
        self.age(rec["id"])
        self.assertEqual(mh.reconcile(rec["id"])["result"], "reconciled")
        self.assertEqual(mh.reconcile(rec["id"])["result"], "reconciled")  # rollback-pending: allowed

    def test_reconcile_noop_on_open(self):
        prop = self.place()
        self.assertEqual(mh.reconcile(prop["id"])["result"], "noop")

    def test_bak_is_0600_from_creation(self):
        prop = self.place()
        modes = []
        real_replace = os.replace
        def spy(src, dst):
            if str(dst).endswith(".bak"):
                modes.append(os.stat(src).st_mode & 0o777)
            return real_replace(src, dst)
        with patch.object(mh.os, "replace", spy):
            mh.attach_card(prop["id"], "x")
        self.assertEqual(modes, [0o600])

    def test_cli_corrupt_registry_exit_2(self):
        self.reg.parent.mkdir(parents=True, exist_ok=True)
        self.reg.write_text("garbage")
        env = dict(os.environ, OMNI_HOLDS_REGISTRY=str(self.reg))
        r = subprocess.run([sys.executable, str(SCRIPTS / "manage_holds.py"), "list"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("registry unreadable", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.reg.read_text(), "garbage")

    def test_cli_place_and_list_roundtrip(self):
        env = dict(os.environ, OMNI_HOLDS_REGISTRY=str(self.reg))
        fake = Path(self.tmp.name) / "gog"
        fake.write_text("#!/bin/sh\necho '{\"event\":{\"id\":\"cli1\"}}'\n")
        fake.chmod(0o755)
        env["OMNI_GOG_BIN"] = str(fake)
        out = subprocess.run([sys.executable, str(SCRIPTS / "manage_holds.py"), "place", "--person", "Jane Doe",
                              "--topic", "Sync", "--thread-ref", '{"kind":"email","threadId":"t"}', "--slot", f"{SLOTS[0]['start']}/{SLOTS[0]['end']}"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        prop = json.loads(out.stdout)
        self.assertEqual(prop["slots"][0]["event_id"], "cli1")
        lst = subprocess.run([sys.executable, str(SCRIPTS / "manage_holds.py"), "list"],
                             capture_output=True, text=True, env=env)
        self.assertEqual([p["id"] for p in json.loads(lst.stdout)], [prop["id"]])


class CliTests(HoldTests):
    """The CLI counts from 1 (a person's or a model's count); the library stays 0-based."""
    def cli(self, *argv):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mh.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def kept(self, pid):
        return [i for i, sl in enumerate(mh.get(pid)["slots"]) if not sl.get("deleted")]

    def test_confirm_1_keeps_the_first_slot_and_len_keeps_the_last(self):
        for n, want in ((1, [0]), (3, [2]), (2, [1])):
            prop = self.place()
            code, _, err = self.cli("confirm", prop["id"], str(n))
            self.assertEqual(code, 0, err)
            self.assertEqual(self.kept(prop["id"]), want)
            self.assertEqual(mh.get(prop["id"])["confirmed_slot"]["event_id"], prop["slots"][want[0]]["event_id"])

    def test_zero_and_too_big_are_rejected_and_change_nothing(self):
        prop = self.place()
        before = len(self.cal.of("delete"))
        for n in ("0", "4", "-1"):
            code, _, err = self.cli("confirm", prop["id"], n)
            self.assertEqual(code, 1)
            self.assertIn("from 1 to 3", err)
        code, _, err = self.cli("release", prop["id"], "--keep", "0", "--reason", "x")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.cal.of("delete")), before)
        self.assertEqual(mh.get(prop["id"])["status"], "open")

    def test_release_keep_is_one_based(self):
        prop = self.place()
        code, _, err = self.cli("release", prop["id"], "--keep", "2", "--reason", "x")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kept(prop["id"]), [1])

    def test_place_requires_a_thread_ref_and_attach_approval_records_the_id(self):
        slot = "%s/%s" % (SLOTS[0]["start"], SLOTS[0]["end"])
        with self.assertRaises(SystemExit):
            self.cli("place", "--person", "Jane", "--slot", slot)
        code, _, err = self.cli("place", "--person", "Jane", "--slot", slot, "--thread-ref", "[1]")
        self.assertEqual(code, 1)
        code, out, err = self.cli("place", "--person", "Jane", "--slot", slot, "--thread-ref", '{"kind":"message","roomId":"!r:x"}')
        self.assertEqual(code, 0, err)
        pid = json.loads(out)["id"]
        code, out, _ = self.cli("attach-approval", pid, "apr_123")
        self.assertEqual(code, 0)
        self.assertEqual(mh.get(pid)["approval_id"], "apr_123")
        self.assertEqual(self.cli("attach-approval", "prop_nope", "a")[0], 1)


if __name__ == "__main__":
    unittest.main()


class GuestSafeReleaseTests(unittest.TestCase):
    """I-5: a hold he added guests to is a real meeting: no release reason ever deletes it."""
    setUp, tearDown, place = HoldTests.setUp, HoldTests.tearDown, HoldTests.place
    REASONS = ("past", "card-dismissed", "card-expired", "card-done", "card-other-option", "approval-cancelled",
               "approval-expired", "counter", "declined", "superseded", "rollback", "manual")

    def guest(self, eid):
        self.cal.events[eid]["attendees"] = [{"email": "jane@example.com"}]

    def test_every_release_reason_keeps_a_hold_with_guests(self):
        for reason in self.REASONS:
            prop = self.place()
            ids = [s["event_id"] for s in prop["slots"]]
            self.guest(ids[1])
            out = mh.release(prop["id"], reason=reason)
            self.assertEqual(out["status"], "released", reason)
            self.assertIn(ids[1], self.cal.events, reason)
            self.assertNotIn(ids[0], self.cal.events, reason)
            self.assertNotIn(ids[2], self.cal.events, reason)
            self.assertEqual(out["slots"][1]["kept_reason"], "has-attendees", reason)
            self.assertNotIn(ids[1], [c[3] for c in self.cal.of("delete")], reason)

    def test_confirm_keeps_an_unchosen_hold_with_guests(self):
        prop = self.place()
        ids = [s["event_id"] for s in prop["slots"]]
        self.guest(ids[2])
        out = mh.confirm(prop["id"], 0)
        self.assertEqual(out["status"], "confirmed")
        self.assertIn(ids[2], self.cal.events)
        self.assertNotIn(ids[1], self.cal.events)
        self.assertEqual(out["slots"][2]["kept_reason"], "has-attendees")

    def test_a_rollback_pending_cleanup_keeps_a_hold_with_guests(self):
        prop = self.place()
        ids = [s["event_id"] for s in prop["slots"]]
        data = json.loads(self.reg.read_text())
        data[prop["id"]]["status"] = "rollback-pending"
        self.reg.write_text(json.dumps(data))
        self.guest(ids[0])
        out = mh.release(prop["id"], reason="rollback")
        self.assertEqual(out["status"], "released")
        self.assertIn(ids[0], self.cal.events)
        self.assertEqual(out["slots"][0]["kept_reason"], "has-attendees")

    def test_a_moved_hold_with_guests_is_still_found(self):
        prop = self.place()
        eid = prop["slots"][0]["event_id"]
        a = self.cal.events[eid]["args"]
        lookups = []
        real = self.cal.__call__
        def spy(args):
            if args[:2] == ["calendar", "events"]:
                lookups.append((opt(args, "--from"), opt(args, "--to")))
            return real(args)
        self.guest(eid)
        with patch.object(mh, "_gog", spy):
            mh.release(prop["id"], reason="past")
        self.assertIn(eid, self.cal.events)
        start, end = lookups[-1]
        self.assertLess(start, "2026-08-01")
        self.assertGreater(end, "2027-01-31")

    def test_an_inconclusive_lookup_deletes_nothing_on_an_open_proposal(self):
        prop = self.place()
        self.cal.lookup_fails = True
        with self.assertRaises(mh.LookupInconclusive):
            mh.release(prop["id"], reason="past")
        self.assertEqual(len(self.cal.of("delete")), 0)
        self.assertEqual(mh.get(prop["id"])["status"], "open")
        self.cal.lookup_fails = False
        self.assertEqual(mh.release(prop["id"], reason="past")["status"], "released")
