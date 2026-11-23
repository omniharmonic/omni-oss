#!/usr/bin/env python3
"""omni_health — daily check that Omni's jobs PRODUCED something, not merely exited 0.

Checks: nightly weave note, morning insight note, a tweet draft, proactive runs spread across the day
(finished runs only), proactive runs refused or failed in the last 24 hours (named by reason), scheduling holds that failed in the last 24 hours, and (via Prism) that cards created today reached a device as a
sent push. Prints JSON; on any failure
sends one plain line per failure to the owner's own Telegram (send_telegram.py, no chat argument).
Always exits 0: the alert is the signal.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    from proactive_intelligence.state import REFUSED_TOOLS_SQL, RUN_FAILURES_SQL  # noqa: E402
    _STATE_IMPORT_ERROR = None
except Exception as _e:  # the health job must still run, and say why it cannot read the runs
    REFUSED_TOOLS_SQL = RUN_FAILURES_SQL = None
    _STATE_IMPORT_ERROR = "%s: %s" % (type(_e).__name__, _e)
DEN = ZoneInfo(os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC")  # The owner's zone (name kept from the original)
# Prism serves it on the hook router (service-token guard): routes/omni.ts GET /hooks/health-summary.
HEALTH_PATH = "/api/omni/hooks/health-summary"
MIN_HOURS = 6
Check = namedtuple("Check", "name ok detail")


def _note(client, name, path):
    try:
        found = client.get_note(path, include_content=False) is not None
    except Exception as e:
        return Check(name, False, "lookup failed (%s)" % type(e).__name__)
    return Check(name, found, "found" if found else "missing " + path)


def _tweet(db, day):
    # The run result does not record a held-back draft, so ignore-only is a failure and a held draft
    # that still carries a non-ignore action passes (limitation).
    row = db.execute("SELECT result FROM runs WHERE day=? AND routine='tweet' AND status='done' ORDER BY started DESC", (day,)).fetchone()
    if not row:
        return Check("tweet", False, "no finished tweet run today")
    try:
        decisions = json.loads(row[0] or "{}").get("decisions") or []
        acts = [d.get("action") for d in decisions if isinstance(d, dict)]
    except (ValueError, AttributeError):
        return Check("tweet", False, "tweet run result unreadable")
    if any(a and a != "ignore" for a in acts):
        return Check("tweet", True, "draft prepared")
    return Check("tweet", False, "tweet run produced no draft (all ignore)")


def _spread(db, day):
    # Only runs that finished: a run refused at start-up (Hermes drift, a guard setting) still fills its hour.
    hours = {datetime.fromtimestamp(r[0] / 1000, DEN).hour for r in db.execute(
        "SELECT started FROM runs WHERE day=? AND bucket='ordinary' AND routine IS NULL AND status='done'", (day,))}
    n = len({h for h in hours if 8 <= h <= 20})
    return Check("proactive-spread", n >= MIN_HOURS, "%d distinct hours (need %d)" % (n, MIN_HOURS))


HOLD_WINDOW_MS = 24 * 3600 * 1000


def _holds(db, now):
    """Hold failures in the last 24h, named by kind. The same predicate as proactive_intelligence.state.State.hold_failures
    (any '*-failed' scheduling line, plus releases the calendar could not confirm), which the job receipt reports as
    health.holdFailures. A state db from before scheduling (no scheduling_log table) has no holds to fail."""
    try:
        rows = db.execute("SELECT kind, count(*) FROM scheduling_log WHERE at>=? AND (kind LIKE '%-failed' OR kind='release-inconclusive') "
                          "GROUP BY kind ORDER BY kind", (now - HOLD_WINDOW_MS,)).fetchall()
    except sqlite3.OperationalError as e:
        if 'no such table' in str(e):
            return Check("holds", True, "no scheduling log yet")
        raise
    total = sum(n for _k, n in rows)
    if not total:
        return Check("holds", True, "no hold failures in 24h")
    return Check("holds", False, "%d hold failure%s in 24h (%s)" % (total, "" if total == 1 else "s", ", ".join("%s %d" % r for r in rows)))


TRANSIENT_REASONS = frozenset({"other", "timeout", "lost"})
TRANSIENT_ALERT_AT = 3


def _failures(db, now):
    """Proactive runs refused or failed in the last 24h, by reason: drift, inline_shell, write_approval,
    audit-unavailable, tool-outside-allowlist (a refused run, or calls a finished run answered with a tool error),
    timeout, lost (the run never reported back) or other. The same queries as State.run_failures."""
    if RUN_FAILURES_SQL is None:
        return Check("proactive-failures", False, "health cannot read proactive runs (%s)" % _STATE_IMPORT_ERROR)
    since = now - HOLD_WINDOW_MS
    reasons = {}
    for reason, n in db.execute(RUN_FAILURES_SQL, (since,)):
        reasons[reason] = reasons.get(reason, 0) + n
    try:
        refused = db.execute(REFUSED_TOOLS_SQL, (since,)).fetchone()[0] or 0
    except sqlite3.OperationalError:
        refused = 0
    # A refused or failed RUN for a guard reason alerts at once. Transient trouble (a provider blip, a timeout, a
    # run that never reported back) alerts from TRANSIENT_ALERT_AT in 24h. Tool calls a finished run answered with a
    # tool error are detail only: the run still did its job.
    transient = sum(n for r, n in reasons.items() if r in TRANSIENT_REASONS)
    alert = any(r not in TRANSIENT_REASONS for r in reasons) or transient >= TRANSIENT_ALERT_AT
    if refused:
        reasons["tool-outside-allowlist"] = reasons.get("tool-outside-allowlist", 0) + refused
    if not reasons:
        return Check("proactive-failures", True, "no refused or failed proactive runs in 24h")
    return Check("proactive-failures", not alert, "proactive refusals or failures in 24h: " +
                 ", ".join("%s %d" % kv for kv in sorted(reasons.items())))


def _pushes(prism):
    try:
        data = prism()
        created = int(data["cards"]["createdToday"])
        p = data["pushes"]
        attempts, sent, failed = int(p["attempts"]), int(p["sent"]), int(p.get("failed") or 0)
        reasons = {str(k): int(v) for k, v in (p.get("skippedByReason") or {}).items()}
    except Exception:
        return Check("pushes", False, "prism unreachable")
    if created == 0 or sent >= 1:
        return Check("pushes", True, "%d cards, %d pushes sent" % (created, sent))
    if attempts == 0:
        return Check("pushes", False, "%d cards today but no push attempted in 24h" % created)
    # Off, the kill switch and quiet hours log every attempt as skipped "not-allowed": a deliberate hold.
    if failed == 0 and reasons.get("not-allowed", 0) == attempts:
        return Check("pushes", True, "%d cards, pushes held deliberately (%d not-allowed)" % (created, attempts))
    why = ", ".join("%s %d" % kv for kv in sorted(reasons.items())) or "no skip reason"
    return Check("pushes", False, "%d cards today but no push sent in 24h (%d attempts, %d failed; skipped: %s)" % (created, attempts, failed, why))


def run_checks(client, state_db_path, prism, now):
    day = datetime.fromtimestamp(now / 1000, DEN).date().isoformat()
    checks = [_note(client, "weave", "vault/agent/reports/%s-nightly-vault-weave" % day),
              _note(client, "insight", "vault/agent/insights/%s" % day)]
    try:
        db = sqlite3.connect("file:%s?mode=ro" % state_db_path, uri=True)
        try:
            checks += [_tweet(db, day), _spread(db, day), _failures(db, now), _holds(db, now)]
        finally:
            db.close()
    except sqlite3.Error as e:
        detail = "state db unreadable (%s)" % type(e).__name__
        checks += [Check(name, False, detail) for name in ("tweet", "proactive-spread", "proactive-failures", "holds")
                   if name not in {c.name for c in checks}]
    checks.append(_pushes(prism))
    return checks


def build_clients():
    import omni_propose
    from parachute_client import ParachuteClient

    def prism():
        base, token = omni_propose.gateway(), omni_propose._token()
        if not base or not token:
            raise RuntimeError("gateway not configured")
        req = urllib.request.Request(base + HEALTH_PATH, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    return ParachuteClient(), prism


def _alert(text):
    """One message to the owner's own Telegram. A failed send is reported on stderr (the job log)."""
    try:
        r = subprocess.run([sys.executable, str(HERE / "send_telegram.py")], input=text, text=True, timeout=60, check=False)
        code = getattr(r, "returncode", 0)
    except Exception as e:
        code = type(e).__name__
    if code:
        print("omni_health: send_telegram.py failed (%s); alert was:\n%s" % (code, text), file=sys.stderr)


def _main(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", type=Path, default=HERE.parent / "state/proactive.sqlite3")
    p.add_argument("--now", type=int, help="epoch ms (tests)")
    a = p.parse_args(argv)
    now = a.now or int(datetime.now(DEN).timestamp() * 1000)
    client, prism = build_clients()
    checks = run_checks(client, a.state, prism, now)
    print(json.dumps([c._asdict() for c in checks], indent=2))
    failed = [c for c in checks if not c.ok]
    if failed:
        _alert("Omni health check failed:\n" + "\n".join("%s: %s" % (c.name, c.detail) for c in failed))


def main(argv=None):
    # Any exception (imports, client construction, a check) is itself an alert; the job always exits 0.
    try:
        import system_path  # Hermes' cron worker PATH lacks Homebrew/system dirs
        system_path.ensure()
        _main(argv)
    except Exception as e:
        _alert("Omni health check itself failed: %s" % type(e).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
