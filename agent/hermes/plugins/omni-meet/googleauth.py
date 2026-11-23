"""Omni's own Google sign-in for meetings: one live Chrome profile, never a copied session.

Two lessons, both from 2026-11-13:

1. Google refuses to sign in a browser a program is driving ("This browser or app may not be
   secure"). So the sign-in happens in plain Google Chrome, started as an ordinary process on a profile
   only Omni uses (``omni-chrome-profile``, 0700): ``open_for_sign_in``. The owner signs in as
   agent@example.com and quits that window.
2. Google revokes a session whose cookies are replayed from a copy. The first version exported the
   profile's cookies to a storage_state file for the bot; Chrome kept rotating the live cookies in the
   profile, the bot kept presenting the stale copy, and within hours Google signed the account out
   everywhere (profile included). So nothing is copied: the meeting bot runs headless Chrome ON the
   profile itself (bot_launcher.use_profile), the same way the sign-in window did, and Chrome rotates
   and stores the cookies as it would for a person.

``verify`` opens the profile headless and asks Google which accounts are signed in (the endpoint Chrome
itself uses); only Omni's account as the default counts. It records the result (account + time, no
cookies) next to the bot's auth path. Chrome holds a profile lock, so only one Chrome uses the profile at
a time: the conductor verifies before it starts a bot, never during a meeting.

The profile is a guard secret path (omni-bridge paths.secret_dirs): no agent turn can read it.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import rules

CHROME_APP = Path("/Applications/Google Chrome.app")
CHROME = CHROME_APP / "Contents/MacOS/Google Chrome"
PROFILE_NAME = "omni-chrome-profile"
META_NAME = "auth.account.json"
SIGN_IN_URL = "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fmeet.google.com%2F"
#: The endpoint Chrome itself uses to show which Google accounts a profile is signed in to, in order.
LIST_ACCOUNTS = "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard"
SIGN_IN_TIMEOUT_S = 30 * 60
#: Chrome's own flags that a profile Chrome itself wrote cannot live with: the mock keychain cannot
#: decrypt its cookies, and the automation flag is what Google looks for.
PROFILE_DROP_DEFAULTS = ["--use-mock-keychain", "--enable-automation"]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class AuthError(RuntimeError):
    pass


# ── pure helpers (unit-tested) ──────────────────────────────────────────────

def accounts_in(text: str) -> List[str]:
    """Signed-in account addresses, default account first, from a ListAccounts (or account page) body."""
    out: List[str] = []
    for e in _EMAIL_RE.findall(text or ""):
        e = e.lower()
        if e not in out:
            out.append(e)
    return out


def chrome_version(app: Path = CHROME_APP) -> Optional[str]:
    try:
        with open(app / "Contents/Info.plist", "rb") as f:
            v = plistlib.load(f).get("CFBundleShortVersionString")
        return str(v) if v else None
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def user_agent(version: Optional[str]) -> Optional[str]:
    """The user agent desktop Chrome on a Mac sends (Chrome reports a frozen OS version and minor numbers)."""
    m = re.match(r"^(\d+)\.", version or "")
    if not m:
        return None
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/%s.0.0.0 Safari/537.36" % m.group(1))


def chrome_available() -> bool:
    return CHROME.is_file() and os.access(CHROME, os.X_OK)


# ── files ───────────────────────────────────────────────────────────────────

def profile_dir(auth: Path) -> Path:
    return auth.parent / PROFILE_NAME


def meta_path(auth: Path) -> Path:
    return auth.parent / META_NAME


def write_private(path: Path, data: Dict[str, Any]) -> None:
    """Atomic write, 0600 from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_meta(auth: Path) -> Dict[str, Any]:
    try:
        meta = json.loads(meta_path(auth).read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def signed_in_account(auth: Path) -> Optional[str]:
    """Omni's address when the last check of the profile found Omni signed in (and the profile is there).

    Cheap: no browser. The conductor re-checks with Google (``verify``) before every meeting, and a
    signed-out result is recorded here, so this never says "signed in" after Google said otherwise."""
    meta = read_meta(auth)
    account = str(meta.get("account") or "").lower()
    if account != rules.AGENT_EMAIL or not meta.get("signed_in") or not profile_dir(auth).is_dir():
        return None
    return account


def record(auth: Path, accounts: List[str]) -> Optional[str]:
    """Write the result of a check; return Omni's address if it is the default signed-in account."""
    ok = bool(accounts) and accounts[0] == rules.AGENT_EMAIL
    write_private(meta_path(auth), {"account": rules.AGENT_EMAIL, "signed_in": ok, "checked_at": int(time.time()),
                                    "chrome": chrome_version()})
    return rules.AGENT_EMAIL if ok else None


def remove_copied_session(auth: Path) -> bool:
    """Delete a cookie copy left by the first version (a stale copy is what gets a session revoked)."""
    try:
        auth.unlink()
        return True
    except FileNotFoundError:
        return False


# ── browser steps (need the login session's Keychain: the agent host's GUI session) ─

def profile_launch_kwargs(headless: bool = True) -> Dict[str, Any]:
    return {"executable_path": str(CHROME), "headless": headless, "ignore_default_args": list(PROFILE_DROP_DEFAULTS),
            "args": ["--disable-blink-features=AutomationControlled"], "user_agent": user_agent(chrome_version())}


def open_for_sign_in(auth: Path, wait: bool = True, popen: Callable[..., Any] = subprocess.Popen) -> Any:
    """Start plain Chrome on Omni's profile at Google's sign-in page; by default wait until it quits."""
    if not chrome_available():
        raise AuthError("Google Chrome is not installed in /Applications")
    prof = profile_dir(auth)
    prof.mkdir(parents=True, exist_ok=True)
    os.chmod(prof, 0o700)
    proc = popen([str(CHROME), "--user-data-dir=%s" % prof, "--no-first-run", "--no-default-browser-check",
                  "--new-window", SIGN_IN_URL],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wait:
        try:
            proc.wait(timeout=SIGN_IN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.terminate()
            raise AuthError("Chrome was left open for 30 minutes; nothing was saved")
    return proc


def signed_in_accounts(auth: Path, playwright_factory: Optional[Callable[[], Any]] = None) -> List[str]:
    """Ask Google, from inside Omni's profile, which accounts are signed in (default first)."""
    prof = profile_dir(auth)
    if not prof.is_dir():
        raise AuthError("no sign-in profile yet: run the sign-in step first")
    if playwright_factory is None:
        from playwright.sync_api import sync_playwright as playwright_factory
    with playwright_factory() as pw:
        ctx = pw.chromium.launch_persistent_context(str(prof), **profile_launch_kwargs())
        try:
            accounts: List[str] = []
            try:
                resp = ctx.request.get(LIST_ACCOUNTS, timeout=30_000)
                if resp.ok:
                    accounts = accounts_in(resp.text())
            except Exception:  # noqa: BLE001 — fall back to the account page
                accounts = []
            if not accounts:
                page = ctx.new_page()
                page.goto("https://myaccount.google.com/", wait_until="domcontentloaded", timeout=30_000)
                accounts = accounts_in(page.content())
        finally:
            ctx.close()
    return accounts


def verify(auth: Path, playwright_factory: Optional[Callable[[], Any]] = None) -> str:
    """Check the profile with Google and record the result. Returns Omni's address; raises AuthError
    (after recording "signed out") when Omni is not the default signed-in account."""
    accounts = signed_in_accounts(auth, playwright_factory)
    if record(auth, accounts):
        remove_copied_session(auth)
        return rules.AGENT_EMAIL
    if not accounts:
        raise AuthError("Chrome is not signed in to any Google account")
    raise AuthError("the signed-in Google account is not Omni's (another account is the default); sign out of it "
                    "and sign in as %s only" % rules.AGENT_EMAIL)
