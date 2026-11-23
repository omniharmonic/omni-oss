"""The meeting bot's entry point when Omni starts it — run as a script in the bot's own process.

It disables the stock bot's conversational voice (google_meet's RealtimeSession, which sends text to
OpenAI's Realtime model and plays the model's REPLY), then runs the stock bot unchanged:

* ``connect`` opens nothing: no OpenAI connection, no key needed (the parent passes a placeholder
  so the bot's "has a key" check sets up the audio bridge), and nothing leaves the machine;
* ``speak`` refuses: the bot's own say-queue can never produce audio;
* ``cancel_response`` / ``close`` do nothing.

The bot still tails ``speaker.pcm`` into its virtual microphone; Omni's speech module is the only
writer of that file. If google_meet's shape changed so the patch cannot be applied, the bot is run
listen-only (``HERMES_MEET_MODE=transcribe``: Chrome's silent fake microphone) — speaking fails
closed, listening still works.
"""

from __future__ import annotations

import os
import runpy
import sys

REQUIRED = ("connect", "speak", "cancel_response", "close")

#: Kept in step with googleauth.CHROME / googleauth.user_agent (a test checks): this file runs as a bare
#: script (``python -P bot_launcher.py``), outside the plugin package, so it cannot import them.
CHROME_APP = "/Applications/Google Chrome.app"
CHROME = CHROME_APP + "/Contents/MacOS/Google Chrome"


def chrome_user_agent(app: str = CHROME_APP):
    import plistlib
    import re
    try:
        with open(app + "/Contents/Info.plist", "rb") as f:
            version = str(plistlib.load(f).get("CFBundleShortVersionString") or "")
    except Exception:  # noqa: BLE001
        return None
    m = re.match(r"^(\d+)\.", version)
    if not m:
        return None
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/%s.0.0.0 Safari/537.36" % m.group(1))


def use_installed_chrome(bot, chrome: str = CHROME) -> bool:
    """Run the bot in installed Google Chrome, presenting as that Chrome on this Mac.

    Omni's Google session was created in this Chrome (googleauth); presenting the same session from the
    stock bot's Chromium with a made-up Linux user agent is what Google treats as a stolen cookie. Off
    when Chrome is missing or the bot's shape changed — the stock browser is used then."""
    ua = chrome_user_agent()
    if not (os.path.isfile(chrome) and os.access(chrome, os.X_OK) and ua):
        return False
    if not callable(getattr(bot, "chromium_executable", None)) or not isinstance(getattr(bot, "_CONTEXT_ARGS", None), dict):
        return False
    bot.chromium_executable = lambda *a, **k: chrome
    bot._CONTEXT_ARGS = dict(bot._CONTEXT_ARGS, user_agent=ua)
    return True


def disable_conversational_voice(module) -> bool:
    cls = getattr(module, "RealtimeSession", None)
    if cls is None or not all(callable(getattr(cls, name, None)) for name in REQUIRED):
        return False

    def connect(self):
        self._ws = None

    def speak(self, text, timeout=30.0):
        return {"ok": False, "error": "omni-meet: the bot does not speak; Omni's approved speech path does"}

    def cancel_response(self):
        return False

    def close(self):
        self._ws = None

    cls.connect, cls.speak, cls.cancel_response, cls.close = connect, speak, cancel_response, close
    cls.__omni_meet_local__ = True
    return True


# ── camera: Omni never shows video ──────────────────────────────────────────
#
# The stock bot launches Chrome with --use-fake-device-for-media-stream, whose camera is Chrome's moving
# green test pattern — what meeting guests saw on 2026-11-13. Two layers, either is enough:
#   1. before joining, press Meet's "Turn off camera" (pre-join screen);
#   2. the fake camera itself shows one black frame (--use-file-for-fake-video-capture), so even a missed
#      click shows nothing.

CAMERA_OFF_LABELS = ("Turn off camera", "Turn off video")
CAMERA_IS_OFF_LABELS = ("Turn on camera", "Turn on video")


def black_video(path: str, width: int = 320, height: int = 180) -> str:
    """One black frame as Y4M (4:2:0): Chrome loops it as the fake camera."""
    with open(path, "wb") as f:
        f.write(b"YUV4MPEG2 W%d H%d F1:1 Ip A1:1 C420jpeg\nFRAME\n" % (width, height))
        f.write(bytes([16]) * (width * height) + bytes([128]) * (2 * (width // 2) * (height // 2)))
    return path


def _label_button(page, labels):
    for label in labels:
        try:
            loc = page.locator('[role="button"][aria-label*="%s" i], button[aria-label*="%s" i]' % (label, label)).first
            if loc.count() and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def camera_off(page, timeout: float = 15.0, sleep=None) -> bool:
    """Press "Turn off camera" on the pre-join screen; True once Meet shows the camera as off."""
    import time
    sleep = sleep or time.sleep
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _label_button(page, CAMERA_IS_OFF_LABELS) is not None:
            return True
        btn = _label_button(page, CAMERA_OFF_LABELS)
        if btn is not None:
            try:
                btn.click(timeout=3_000)
            except Exception:  # noqa: BLE001
                pass
        sleep(0.5)
    return _label_button(page, CAMERA_IS_OFF_LABELS) is not None


def keep_camera_off(bot) -> bool:
    """Run camera_off before the bot's own join; refuse (False) if the bot's shape changed."""
    original = getattr(bot, "_join", None)
    if not callable(original):
        return False

    def _join(page, cfg, state, *args, **kwargs):
        off = camera_off(page)
        sys.stderr.write("omni-meet: camera %s before joining\n" % ("off" if off else "NOT confirmed off (black frame only)"))
        return original(page, cfg, state, *args, **kwargs)
    bot._join = _join
    return True


#: Kept in step with googleauth.PROFILE_NAME / PROFILE_DROP_DEFAULTS (a test checks).
PROFILE_NAME = "omni-chrome-profile"
PROFILE_DROP_DEFAULTS = ["--use-mock-keychain", "--enable-automation"]


def profile_for(auth_state: str):
    """Omni's live Chrome profile beside the bot's auth path, when the parent said Omni is signed in
    (it passes HERMES_MEET_AUTH_STATE only then) and the profile exists."""
    if not auth_state:
        return None
    prof = os.path.join(os.path.dirname(auth_state), PROFILE_NAME)
    return prof if os.path.isdir(prof) else None


class _ProfileBrowser:
    """Stands in for the Browser the stock bot launches: its one context IS Omni's profile.

    The bot calls ``browser.new_context(**context_args)`` (viewport, user agent, permissions and a
    ``storage_state`` path); a persistent profile has its own cookies, so storage_state is ignored —
    there is no cookie copy to replay (googleauth, lesson 2)."""

    def __init__(self, ctx):
        self._ctx = ctx
        self._closed = False

    def new_context(self, **kwargs):
        perms = kwargs.get("permissions")
        if perms:
            try:
                self._ctx.grant_permissions(list(perms), origin="https://meet.google.com")
            except Exception:  # noqa: BLE001
                pass
        return self._ctx

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._ctx.close()
            except Exception:  # noqa: BLE001 — the bot closes the context first
                pass


#: Opt-in (meeting-assistant spec, P0 spike S2): Playwright adds ``--mute-audio`` to every headless launch,
#: so the call is silent on the agent host and a recorder (Minutes) captures nothing. Off by default: with the
#: agent host's default output on BlackHole 2ch — Omni's microphone when speaking — unmuted call audio could
#: loop back into the call. Only ``1`` turns it on.
UNMUTE_ENV = "OMNI_MEET_UNMUTE_AUDIO"


def audio_unmuted(env=None) -> bool:
    return (os.environ if env is None else env).get(UNMUTE_ENV, "").strip() == "1"


def drop_defaults(base, env=None) -> list:
    """Playwright default flags to remove from a launch: ``base`` plus ``--mute-audio`` when unmuted."""
    drop = list(base)
    if audio_unmuted(env) and "--mute-audio" not in drop:
        drop.append("--mute-audio")
    return drop


def patch_launch(out_dir: str, profile=None, user_agent=None) -> bool:
    """Every Chromium launch in this process: a black frame as the fake camera, and — when Omni is signed
    in — the launch opens Omni's live profile instead of a fresh browser."""
    try:
        import playwright.sync_api as api
    except Exception:  # noqa: BLE001
        return False
    real = api.sync_playwright
    video = black_video(os.path.join(out_dir, "omni-black.y4m"))

    class _Manager:
        def __init__(self):
            self._cm = real()

        def __enter__(self):
            pw = self._cm.__enter__()
            launch = pw.chromium.launch

            def launch_omni(*args, **kwargs):
                kwargs["args"] = list(kwargs.get("args") or []) + ["--use-file-for-fake-video-capture=%s" % video]
                if not profile:
                    if audio_unmuted():
                        kwargs["ignore_default_args"] = drop_defaults(kwargs.get("ignore_default_args") or [])
                    return launch(*args, **kwargs)
                kwargs.pop("channel", None)
                ctx = pw.chromium.launch_persistent_context(
                    profile, executable_path=kwargs.get("executable_path"), headless=kwargs.get("headless", True),
                    args=kwargs["args"], ignore_default_args=drop_defaults(PROFILE_DROP_DEFAULTS),
                    viewport={"width": 1280, "height": 800}, user_agent=user_agent)
                return _ProfileBrowser(ctx)
            pw.chromium.launch = launch_omni
            return pw

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

    api.sync_playwright = _Manager
    return True


# ── captions: the bot can only hear what Meet captions ON ITS OWN PAGE ─────
#
# The stock bot dispatches one synthetic "c" keydown right after clicking join (often before it is
# admitted); Meet ignores untrusted key events and nothing retries — on 2026-11-13 Omni sat in a call for
# 43 minutes with 0 caption lines. While in the call, this keeps captions on with a real click (or a real
# keypress) and re-installs the bot's caption observer if the page lost it.

#: Meet's captions as rendered on 2026-11-13 (seen by a probe on the agent host):
#:   <div role="region" aria-label="Captions">            the panel
#:     <div class="nMcdL">                                one block per speaker turn
#:       <div class="KcIKyf"><span class="NWpY1d">Name</span></div>
#:       <div class="ygicle">the whole turn, growing as they speak, re-punctuated as Meet revises it</div>
#: The stock observer looks for rows marked jsname="dsyhDe" — now the wrapper AROUND the panel — finds
#: none, and records the panel's last line ("Jump to bottom") with no speaker. This one reads the blocks
#: and emits each finished sentence once: finished = followed by more text, or the block quiet for 2 s.
#: Sentences are de-duplicated ignoring case and punctuation, so Meet re-punctuating "Omni, can you
#: hear me?" as "Omni. Can you hear me?" never looks like a second question.
OMNI_CAPTION_OBSERVER_JS = r"""
(() => {
  if (window.__hermesMeetInstalled) return;
  window.__hermesMeetInstalled = true;
  window.__hermesMeetQueue = window.__hermesMeetQueue || [];
  const STABLE_MS = 2000, RECENT = 4;
  const state = new WeakMap();
  const region = () => document.querySelector('[role="region"][aria-label="Captions"], [role="region"][aria-label*="aption" i]');
  const norm = (s) => s.toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, ' ').replace(/\s+/g, ' ').trim();
  function blocks(r) {
    const b = [...r.querySelectorAll('div.nMcdL')];
    return b.length ? b : [...r.children].filter((c) => !c.querySelector('button') && (c.innerText || '').trim());
  }
  function parts(b) {
    const spk = b.querySelector('.NWpY1d, .KcIKyf');
    let txt = b.querySelector('.ygicle, .VbkSUe');
    if (!txt) {   // layout drift: the longest text-bearing descendant that is not the name
      let best = null;
      for (const d of b.querySelectorAll('div, span')) {
        if (spk && (d === spk || spk.contains(d) || d.contains(spk))) continue;
        if (!best || (d.innerText || '').length > (best.innerText || '').length) best = d;
      }
      txt = best;
    }
    return [(spk ? spk.innerText : '').trim(), (txt ? txt.innerText : '').trim()];
  }
  function scan() {
    const r = region();
    if (!r) return;
    const now = Date.now();
    for (const b of blocks(r)) {
      const [speaker, text] = parts(b);
      if (!text) continue;
      let st = state.get(b);
      if (!st) { st = { sent: new Set(), last: '', at: now }; state.set(b, st); }
      if (text !== st.last) { st.last = text; st.at = now; }
      const stable = now - st.at >= STABLE_MS;
      const done = (text.match(/[^.?!]*[.?!]+/g) || []).map((x) => x.trim()).filter(Boolean);
      const rest = text.slice((text.match(/^(?:[^.?!]*[.?!]+)*/) || [''])[0].length).trim();
      let ready = (rest || stable) ? done.slice() : done.slice(0, -1);
      if (stable && rest) ready.push(rest);
      ready = ready.slice(-RECENT);
      for (const sentence of ready) {
        const key = norm(sentence);
        if (!key || st.sent.has(key)) continue;
        st.sent.add(key);
        window.__hermesMeetQueue.push({ ts: now, speaker, text: sentence });
      }
    }
  }
  setInterval(scan, 1000);
  window.__hermesMeetDrain = () => { const out = window.__hermesMeetQueue.slice(); window.__hermesMeetQueue = []; return out; };
})();
"""

CAPTION_CHECK_EVERY_S = 5.0
CAPTION_BUTTON_LABELS = ("Captions and translation", "Turn on captions", "Captions")


class CaptionKeeper:
    def __init__(self, bot, clock=None):
        import time
        self.bot = bot
        self.clock = clock or time.time
        self.last = 0.0
        self.attempts = 0
        self.on = False

    def in_call(self, page) -> bool:
        try:
            return bool(page.locator('button[aria-label*="eave call" i]').count())
        except Exception:  # noqa: BLE001
            return False

    def captions_on(self, page) -> bool:
        try:
            return bool(page.evaluate(
                "!!(document.querySelector('[role=\"region\"][aria-label=\"Captions\"]') || "
                "[...document.querySelectorAll('button[aria-pressed=\"true\"]')].some(b => /caption/i.test(b.getAttribute('aria-label') || '')))"))
        except Exception:  # noqa: BLE001
            return False

    def tick(self, page) -> None:
        now = self.clock()
        if now - self.last < CAPTION_CHECK_EVERY_S:
            return
        self.last = now
        if not self.in_call(page):
            return
        if self.captions_on(page):
            if not self.on:
                self.on = True
                sys.stderr.write("omni-meet: captions on\n")
        else:
            self.on = False
            self.attempts += 1
            # Meet's "c" shortcut toggles captions (proved on the agent host); the button is the fallback.
            # Only ever pressed while captions are OFF, so it can never toggle them off.
            how = "key c"
            try:
                if self.attempts % 2:
                    page.keyboard.press("c")
                else:
                    btn = _label_button(page, CAPTION_BUTTON_LABELS)
                    if btn is not None:
                        btn.click(timeout=3_000)
                        how = "button"
                    else:
                        page.keyboard.press("c")
            except Exception:  # noqa: BLE001
                pass
            if self.attempts in (1, 5, 20):
                sys.stderr.write("omni-meet: turning captions on (attempt %d, %s)\n" % (self.attempts, how))
        try:
            if not page.evaluate("!!window.__hermesMeetInstalled"):
                page.evaluate(OMNI_CAPTION_OBSERVER_JS)
        except Exception:  # noqa: BLE001
            pass
        self.write_presence(page)

    def write_presence(self, page) -> None:
        """How many people are in the call (Omni included), for the conductor: Omni may say anything only
        when it is just the owner and Omni. Unknown is written as null, which the conductor treats as
        "others present"."""
        out_dir = os.environ.get("HERMES_MEET_OUT_DIR")
        if not out_dir:
            return
        try:
            seen = page.evaluate(PRESENCE_JS)
        except Exception:  # noqa: BLE001
            seen = None
        count = presence_count(seen)
        try:
            import json
            import time
            tmp = os.path.join(out_dir, ".omni-presence.tmp")
            with open(tmp, "w") as f:
                json.dump({"count": count, "at": time.time(), "seen": seen}, f)
            os.replace(tmp, os.path.join(out_dir, "omni-presence.json"))
        except Exception:  # noqa: BLE001
            pass


#: Two independent readings of who is in the call: the participant tiles (one data-participant-id each)
#: and the number on the People button. presence_count trusts them only when they agree, or when only
#: one is readable.
PRESENCE_JS = r"""(() => {
  const ids = new Set([...document.querySelectorAll('[data-participant-id]')].map(e => e.getAttribute('data-participant-id')).filter(Boolean));
  let badge = null;
  for (const b of document.querySelectorAll('button[aria-label], [role="button"][aria-label]')) {
    const label = b.getAttribute('aria-label') || '';
    if (!/people|everyone|participant/i.test(label)) continue;
    const m = (label + ' ' + (b.innerText || '')).match(/\b(\d{1,3})\b/);
    if (m) { badge = parseInt(m[1], 10); break; }
  }
  return { tiles: ids.size, badge };
})()"""


def presence_count(seen) -> "int | None":
    if not isinstance(seen, dict):
        return None
    tiles = seen.get("tiles") if isinstance(seen.get("tiles"), int) and seen.get("tiles") > 0 else None
    badge = seen.get("badge") if isinstance(seen.get("badge"), int) and seen.get("badge") > 0 else None
    if tiles and badge:
        return tiles if tiles == badge else None   # disagree: unknown (treated as not alone)
    return tiles or badge


class _KeptPage:
    """The page the bot's drain loop sees: identical, except each caption drain first lets the keeper
    run on the same thread (Playwright's sync API is single-threaded)."""

    def __init__(self, page, keeper):
        self._page = page
        self._keeper = keeper

    def evaluate(self, js, *args, **kwargs):
        if isinstance(js, str) and "__hermesMeetDrain" in js:
            try:
                self._keeper.tick(self._page)
            except Exception:  # noqa: BLE001
                pass
        return self._page.evaluate(js, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._page, name)


def keep_captions_on(bot) -> bool:
    original = getattr(bot, "_drain_loop", None)
    if not callable(original):
        return False
    # The bot installs its caption observer by evaluating this attribute right after joining.
    bot._CAPTION_OBSERVER_JS = OMNI_CAPTION_OBSERVER_JS

    def _drain_loop(page, *args, **kwargs):
        return original(_KeptPage(page, CaptionKeeper(bot)), *args, **kwargs)
    bot._drain_loop = _drain_loop
    return True


def main() -> None:
    patched = False
    try:
        from plugins.google_meet.realtime import openai_client
        patched = disable_conversational_voice(openai_client)
    except Exception as exc:  # noqa: BLE001 — never let a patch failure stop listening
        sys.stderr.write("omni-meet: voice patch unavailable (%s)\n" % exc)
    if not patched:
        os.environ["HERMES_MEET_MODE"] = "transcribe"
        sys.stderr.write("omni-meet: listening only (could not disable the bot's own voice)\n")
    else:
        sys.stderr.write("omni-meet: bot voice is Omni's speech path only\n")
    try:
        import importlib
        bot = importlib.import_module("plugins.google_meet.meet_bot")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("omni-meet: cannot import the bot (%s); running it unmodified\n" % exc)
        bot = None
    if bot is not None and callable(getattr(bot, "run_bot", None)):
        sys.stderr.write("omni-meet: browser is %s\n" % ("installed Google Chrome" if use_installed_chrome(bot)
                                                          else "the bot's own Chromium"))
        out_dir = os.environ.get("HERMES_MEET_OUT_DIR") or "/tmp"
        profile = profile_for(os.environ.get("HERMES_MEET_AUTH_STATE", ""))
        sys.stderr.write("omni-meet: signed in via %s\n" % ("Omni's Chrome profile" if profile else "nothing (guest)"))
        sys.stderr.write("omni-meet: camera off before join %s; launch patch %s; caption keeper %s\n" % (
            "armed" if keep_camera_off(bot) else "UNAVAILABLE",
            "on" if patch_launch(out_dir, profile, chrome_user_agent()) else "UNAVAILABLE",
            "armed" if keep_captions_on(bot) else "UNAVAILABLE"))
        sys.exit(bot.run_bot())
    runpy.run_module("plugins.google_meet.meet_bot", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
