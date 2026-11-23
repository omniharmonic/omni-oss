"""Containment for the tools that are NOT the terminal.

The terminal's command is rewritten to run inside the sandbox (sandbox.wrap).
Three other families touch the disk, and a path rule alone cannot hold them:
between the moment a path is judged and the moment it is opened, a process the
turn started can swap what the path points at (a symlink where a file was).
So none of them is left to a path rule alone.

1. Hermes' file tools (`read_file`, `search_files`, `write_file`, `patch`) do
   their work as shell commands on the turn's terminal environment. That
   environment's `execute` is wrapped, for Omni turns, so every one of those
   commands runs inside the same sandbox as a terminal command: a secret does
   not open and a write outside the workspace does not happen, whatever the
   path turned into. `search_files` over a whole home directory skips the
   secrets it meets for the same reason (tolerant_search: the refusals are
   dropped from its output instead of failing the whole search).

2. A tool that reads a local file in Hermes' own process (`vision_analyze`, a
   `file://` address, an attachment) is given a private COPY instead: the file
   is read once, inside the sandbox, into a directory no Omni command can
   write, and the tool is pointed at the copy.

3. A tool that writes a local file in Hermes' own process (`text_to_speech`
   with an output path) writes into a private directory; the result is then
   put where it was asked for by a copy made inside the sandbox.

If the environment cannot be wrapped (a Hermes that changed underneath us, a
remote terminal backend), `guard_env` says so and the caller asks the owner
before each file tool instead.
"""

from __future__ import annotations

import logging
import os
import re
import secrets as _secrets
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import paths, sandbox

logger = logging.getLogger(__name__)

FILE_TOOLS = frozenset({"read_file", "search_files", "write_file", "patch"})
#: Keys whose value is prose or code, never a path to open.
TEXT_KEYS = frozenset({"content", "text", "body", "query", "search", "prompt", "question", "message", "instructions", "goal",
                       "context", "description", "title", "summary", "expression", "code", "old_string", "new_string"})
#: Keys that name a file a tool will WRITE.
WRITE_KEYS = frozenset({"output_path", "save_path", "output_file", "out_path", "destination", "dest", "download_path", "save_to"})
MAX_COPY = 64 * 1024 * 1024

_lock = threading.Lock()
_depth: Dict[str, int] = {}                    # task id → Omni tool calls in flight
_calls: Dict[str, str] = {}                    # tool call id → task id
_grants: Dict[str, Tuple[str, str, str]] = {}  # task id → (tool call id, profile, tier): an approved write outside
_copies: Dict[str, List[str]] = {}             # tool call id → private files to remove afterwards
_outputs: Dict[str, Tuple[str, List[Tuple[str, str]]]] = {}  # tool call id → (profile, [(private file, where it was asked for)])
_shown: Dict[str, List[Tuple[str, str]]] = {}  # tool call id → (private path, the path the model named), for the result text


class Refused(Exception):
    """The file could not be handed over safely; the message is for the model."""


def is_local(value: str) -> bool:
    return bool(re.match(r"^(file://|~|\.{1,2}/|/)", value, re.IGNORECASE)) and len(value) <= 1024 and "\n" not in value


def local_part(value: str) -> str:
    if value.lower().startswith("file://"):
        rest = value[7:]
        if rest.lower().startswith("localhost/"):
            rest = rest[9:]
        return rest.split("?", 1)[0].split("#", 1)[0]
    return value


def strings(value: Any, key: str = "", depth: int = 0):
    """Every (key, string) in a tool's arguments."""
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict) and depth < 6:
        for k, v in value.items():
            yield from strings(v, str(k), depth + 1)
    elif isinstance(value, (list, tuple)) and depth < 6:
        for v in value:
            yield from strings(v, key, depth + 1)


# ── the turn's terminal environment ──────────────────────────────────────────

_SEARCH_CMD = re.compile(r"^\s*(set -o pipefail;\s*)?(rg|grep|find)\s")
_EPERM = ("Operation not permitted", "Permission denied")


def tolerant_search(command: str) -> str:
    """Hermes' search_files command (`rg` / `grep -r` / `find`, optionally after `set -o pipefail;`),
    made to SKIP what the sandbox refuses instead of failing: the "Operation not permitted" lines a
    protected file or directory produces are dropped, and an exit status of 2 caused only by them
    becomes 0. Other errors (a bad pattern) are kept as they were. Anything else: unchanged."""
    if not isinstance(command, str) or not _SEARCH_CMD.match(command) or "\n" in command:
        return command
    pat = " ".join("-e %s" % ("'" + e + "'") for e in _EPERM)
    return ('__omni_se="$(/usr/bin/mktemp -t omni-search)"; { %s ; } 2>"$__omni_se"; __omni_src=$?; '
            'if [ -s "$__omni_se" ]; then /usr/bin/grep -v %s "$__omni_se" >&2; '
            'if [ $__omni_src -eq 2 ] && ! /usr/bin/grep -q -v %s "$__omni_se"; then __omni_src=0; fi; fi; '
            '/bin/rm -f "$__omni_se"; (exit $__omni_src)') % (command, pat, pat)


def enter(task_id: str, tool_call_id: str) -> None:
    """An Omni tool call is about to run on this task."""
    with _lock:
        _depth[task_id] = _depth.get(task_id, 0) + 1
        if tool_call_id:
            if len(_calls) > 2000:
                _calls.clear()
            _calls[tool_call_id] = task_id


def leave(tool_call_id: str) -> None:
    """That call ended: forget it, and remove what was made for it."""
    if not tool_call_id:
        return
    with _lock:
        task_id = _calls.pop(tool_call_id, None)
        if task_id is not None:
            _depth[task_id] = max(0, _depth.get(task_id, 1) - 1)
            g = _grants.get(task_id)
            if g and g[0] == tool_call_id:
                _grants.pop(task_id, None)
        copies = _copies.pop(tool_call_id, [])
    for p in copies:
        _remove(p)


def grant(task_id: str, tool_call_id: str, profile: str, tier: str) -> None:
    """The owner approved this file-tool call: its commands run with this profile."""
    with _lock:
        _grants[task_id] = (tool_call_id, profile, tier)


def guard_env(task_id: str, in_scope: Callable[[], bool]) -> str:
    """Make every command on this task's terminal environment run in the sandbox
    while an Omni call is in flight. 'ok', 'remote' (not a local shell) or 'failed'."""
    try:
        from tools.file_tools import _get_file_ops
        env = _get_file_ops(task_id or "default").env
    except Exception as e:
        logger.warning("omni-bridge: the turn's terminal environment is not reachable (%s)", type(e).__name__)
        return "failed"
    if type(env).__name__ != "LocalEnvironment":
        return "remote"
    if getattr(env, "_omni_guard", None) is True:
        return "ok"
    try:
        original = env.execute

        def execute(command, *args, **kwargs):
            contained = False
            try:
                with _lock:
                    busy = _depth.get(task_id, 0) > 0
                    g = _grants.get(task_id)
                contained = busy or bool(in_scope())
            except Exception:
                contained = True
            if contained and isinstance(command, str) and not command.startswith(sandbox.WRAP_PREFIX):
                # Not already wrapped by the terminal's own rewrite: a file tool's command,
                # or one of Hermes' own. It runs contained (or as the owner approved).
                if not sandbox.available():
                    raise RuntimeError("omni-bridge: no sandbox on this machine; the command was not run")
                prof, tier = (g[1], g[2]) if g else (sandbox.profile("contained"), "contained")
                command = sandbox.wrap(tolerant_search(command), prof, tier=tier)
            return original(command, *args, **kwargs)

        env.execute = execute
        env._omni_guard = True
        return "ok"
    except Exception as e:
        logger.warning("omni-bridge: could not wrap the terminal environment (%s)", type(e).__name__)
        return "failed"


# ── files handed to tools that open them in Hermes' own process ─────────────

def _private(kind: str) -> str:
    d = os.path.join(paths.hermes_home(), "cache", "omni-bridge", kind)
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        now = time.time()
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if now - os.lstat(p).st_mtime > 3600:
                _remove(p)
    except OSError:
        pass
    return d


def _remove(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            for name in os.listdir(path):
                _remove(os.path.join(path, name))
            os.rmdir(path)
        else:
            os.unlink(path)
    except OSError:
        pass


def _ext(path: str) -> str:
    ext = os.path.splitext(path)[1]
    return ext if re.match(r"^\.[A-Za-z0-9]{1,10}$", ext) else ""


def copy_in(path: str) -> str:
    """A private copy of `path`, read once INSIDE the sandbox (so a secret does not
    open, whatever the path points at by then). Returns the copy's path."""
    if not sandbox.available():
        raise Refused("This tool reads a local file directly, and the sandbox that makes that safe is not available here.")
    dst = os.path.join(_private("in"), _secrets.token_hex(12) + _ext(path))
    try:
        with open(dst, "wb") as out:
            r = subprocess.run([sandbox.SANDBOX_EXEC, "-p", sandbox.profile("contained"), "/usr/bin/head", "-c", str(MAX_COPY + 1), "--", path],
                               stdout=out, stderr=subprocess.PIPE, timeout=120, env={"PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError):
        _remove(dst)
        raise Refused("That file could not be read.")
    if r.returncode != 0:
        _remove(dst)
        raise Refused("That file could not be read (it does not exist, or it is not readable from an Omni conversation).")
    if os.path.getsize(dst) > MAX_COPY:
        _remove(dst)
        raise Refused("That file is too large to hand to this tool (over 64 MB).")
    return dst


def copy_out(private: str, dest: str, profile: Optional[str] = None) -> bool:
    """Put a file a tool wrote privately where it was asked for — by a write made INSIDE
    the sandbox (so it lands in the workspace — or where the owner approved — or nowhere)."""
    try:
        with open(private, "rb") as src:
            r = subprocess.run([sandbox.SANDBOX_EXEC, "-p", profile or sandbox.profile("contained"), "/bin/sh", "-c",
                                'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", dest],
                               stdin=src, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120, env={"PATH": "/usr/bin:/bin"})
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _swap(value: Any, key: str, fn: Callable[[str, str], Optional[str]], depth: int = 0) -> Any:
    if isinstance(value, str):
        new = fn(key, value)
        return value if new is None else new
    if isinstance(value, dict) and depth < 6:
        return {k: _swap(v, str(k), fn, depth + 1) for k, v in value.items()}
    if isinstance(value, list) and depth < 6:
        return [_swap(v, key, fn, depth + 1) for v in value]
    return value


def private_paths(args: Dict[str, Any], cwd: Optional[str], tool_call_id: str,
                  write_profile: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The arguments of an in-process tool with every local file swapped for a private
    copy (reads) or a private destination (writes). None when nothing changed.
    Raises Refused when a file cannot be handed over."""
    made: List[str] = []
    outs: List[Tuple[str, str]] = []
    shown: List[Tuple[str, str]] = []

    def one(key: str, value: str) -> Optional[str]:
        if key in TEXT_KEYS or not is_local(value):
            return None
        is_url = value.lower().startswith("file://")
        real = paths.real(local_part(value), cwd)
        if key in WRITE_KEYS:
            d = os.path.join(_private("out"), _secrets.token_hex(12))
            os.makedirs(d, mode=0o700)
            private = os.path.join(d, os.path.basename(real) or "output")
            made.append(d)
            outs.append((private, real))
            shown.append((private, real))
            return private
        if not os.path.lexists(real):
            return None                     # nothing there: the tool will say so itself
        if os.path.isdir(real):
            raise Refused("This tool was given a folder; from an Omni conversation it can only be given a file.")
        copy = copy_in(real)
        made.append(copy)
        shown.append((copy, real))
        return ("file://" + copy) if is_url else copy

    try:
        new = _swap(args, "", one)
    except Exception:
        for p in made:
            _remove(p)
        raise
    if not made:
        return None
    if tool_call_id:
        with _lock:
            if len(_shown) > 500:
                _shown.clear()
            if len(_copies) > 500:
                _copies.clear()
                _outputs.clear()
            _copies[tool_call_id] = made
            _shown[tool_call_id] = shown
            if outs:
                _outputs[tool_call_id] = (write_profile or "", outs)
    elif outs:
        raise Refused("This call has no id, so its output could not be delivered safely. It was not run.")
    return {k: v for k, v in new.items() if v != args.get(k)}


def deliver(tool_call_id: str) -> List[Tuple[str, str, bool]]:
    """After the tool ran: put what it wrote privately where it was asked for.
    Returns (private path, destination, delivered?) for each."""
    with _lock:
        profile, outs = _outputs.pop(tool_call_id, ("", [])) if tool_call_id else ("", [])
    done = []
    for private, dest in outs:
        ok = os.path.isfile(private) and copy_out(private, dest, profile or None)
        if not ok:
            logger.warning("omni-bridge: a tool's output could not be put where it was asked for")
        done.append((private, dest, ok))
    return done


def show_named_paths(tool_call_id: str, text: str) -> str:
    """A tool's result with the private paths replaced by the ones the model named."""
    with _lock:
        pairs = list(_shown.get(tool_call_id, ())) if tool_call_id else []
    for private, named in pairs:
        text = text.replace(private, named)
    return text
