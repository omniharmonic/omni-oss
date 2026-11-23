"""A small client for the Minutes app's control socket (meeting-assistant spec, Phase 2).

Minutes (the Mac recorder) listens on a Unix socket, by default
``~/Library/Application Support/Minutes/control.sock`` (``MINUTES_CONTROL_SOCKET`` overrides it). The
protocol is newline-delimited JSON: one request ``{"id", "method", "params"}`` per connection, one
response ``{"id", "ok": true, "result"}`` or ``{"id", "ok": false, "error": {"code", "message"}}``.

Every call returns the ``result`` dict or raises :class:`MinutesError`. Codes are Minutes' own
(``bad_request``, ``unknown_method``, ``busy``, ``not_ready``, ``permission_denied``, ``not_recording``,
``wrong_meeting``, ``not_found``, ``internal``) plus three of ours: ``unavailable`` (no socket, or
nothing listening — the app is not running), ``timeout`` and ``bad_response``.

Only the conductor calls these. ``start`` / ``stop`` record; ``get`` / ``assign`` / ``clip`` / ``export``
name the speakers afterwards (speakers.py, the conductor's ``naming`` stage). ``OMNI_MEET_MINUTES=off`` is
the rollback: the conductor checks :func:`enabled` and then makes no call at all.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_SOCKET = Path.home() / "Library" / "Application Support" / "Minutes" / "control.sock"
SOCKET_ENV = "MINUTES_CONTROL_SOCKET"
SWITCH_ENV = "OMNI_MEET_MINUTES"

STATUS_TIMEOUT_S = 3.0
CONTROL_TIMEOUT_S = 10.0
#: The agent host may have no real microphone; the call is what plays on the Mac (Chrome, unmuted).
CAPTURE_MODE = "system_audio"
#: Errors that mean "there is nothing of ours to stop" — not a failure when leaving.
STOP_TOLERATED = ("not_recording", "wrong_meeting", "unavailable", "not_found")

_ids = itertools.count(1)


class MinutesError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = str(code or "internal")
        self.message = str(message or "")
        super().__init__("%s: %s" % (self.code, self.message) if self.message else self.code)


def enabled(env=None) -> bool:
    """On unless ``OMNI_MEET_MINUTES`` is off/0/false/no."""
    return (os.environ if env is None else env).get(SWITCH_ENV, "").strip().lower() not in ("off", "0", "false", "no")


def socket_path() -> Path:
    raw = os.environ.get(SOCKET_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_SOCKET


def call(method: str, params: Optional[Dict[str, Any]] = None, timeout: float = CONTROL_TIMEOUT_S) -> Dict[str, Any]:
    req_id = "omni-%d-%d" % (os.getpid(), next(_ids))
    payload = json.dumps({"id": req_id, "method": method, "params": params or {}}) + "\n"
    path = str(socket_path())
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(path)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise MinutesError("unavailable", "Minutes is not running (%s)" % type(exc).__name__) from None
        except socket.timeout:
            raise MinutesError("timeout", "Minutes did not answer within %gs" % timeout) from None
        except OSError as exc:
            raise MinutesError("unavailable", "cannot reach Minutes (%s)" % (exc.strerror or type(exc).__name__)) from None
        buf = b""
        try:
            s.sendall(payload.encode("utf-8"))
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 8 * 1024 * 1024:
                    raise MinutesError("bad_response", "Minutes sent an oversized answer")
        except socket.timeout:
            raise MinutesError("timeout", "Minutes did not answer within %gs" % timeout) from None
        except (ConnectionResetError, BrokenPipeError):
            raise MinutesError("unavailable", "Minutes closed the connection") from None
    finally:
        s.close()
    line = buf.split(b"\n", 1)[0].strip()
    if not line:
        raise MinutesError("bad_response", "Minutes closed the connection without answering")
    try:
        resp = json.loads(line.decode("utf-8"))
    except ValueError:
        raise MinutesError("bad_response", "Minutes sent something that is not JSON") from None
    if not isinstance(resp, dict):
        raise MinutesError("bad_response", "Minutes sent an unexpected answer")
    if resp.get("id") not in (None, req_id):
        raise MinutesError("bad_response", "Minutes answered a different request")
    if resp.get("ok") is True:
        result = resp.get("result")
        return result if isinstance(result, dict) else {}
    err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
    raise MinutesError(str(err.get("code") or "internal"), str(err.get("message") or ""))


def status() -> Dict[str, Any]:
    return call("status", timeout=STATUS_TIMEOUT_S)


def start(title: str, capture_mode: str = CAPTURE_MODE, event_id: Optional[str] = None,
          attendees: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"title": title, "capture_mode": capture_mode}
    if event_id:
        params["event_id"] = event_id
    if attendees:
        params["attendees"] = attendees
    return call("start", params)


def stop(meeting_id: str) -> Dict[str, Any]:
    return call("stop", {"meeting_id": meeting_id})


def mark(label: Optional[str] = None) -> Dict[str, Any]:
    return call("mark", {"label": label} if label else {})


def get(meeting_id: str) -> Dict[str, Any]:
    return call("get", {"meeting_id": meeting_id}, timeout=CONTROL_TIMEOUT_S)


def assign(meeting_id: str, cluster_id: str, name: str, email: Optional[str] = None, learn: bool = False) -> Dict[str, Any]:
    """Name one speaker cluster (the same path as the app's review screen). ``learn`` remembers the voice;
    Minutes refuses it with ``bad_request`` for a cluster it cannot learn from."""
    params: Dict[str, Any] = {"meeting_id": meeting_id, "cluster_id": cluster_id, "name": name, "learn": bool(learn)}
    if email:
        params["email"] = email
    return call("assign", params)


def export(meeting_id: str) -> Dict[str, Any]:
    return call("export", {"meeting_id": meeting_id}, timeout=EXPORT_TIMEOUT_S)


#: Encoding a clip takes Minutes a moment (AAC); the export writes the vault note.
CLIP_TIMEOUT_S = 30.0
EXPORT_TIMEOUT_S = 30.0
CLIP_MAX_SECONDS = 20
CLIP_MIN_SECONDS = 3
#: Prism refuses clips over 600 KB.
CLIP_MAX_BYTES = 600 * 1024
CLIPS_DIR_ENV = "MINUTES_CLIPS_DIR"
#: Prism accepts a clip of more than 0 and at most this many seconds.
CLIP_MAX_DURATION_S = 60.0


def clip(meeting_id: str, cluster_id: str, max_seconds: int = 10) -> Dict[str, Any]:
    """A short m4a of one cluster's clearest speech: ``{"path", "duration_s", "segments"}``. Minutes refuses
    ``max_seconds`` outside 3–20 (``bad_request``), so it is kept inside that range here."""
    secs = max(CLIP_MIN_SECONDS, min(CLIP_MAX_SECONDS, int(max_seconds)))
    return call("clip", {"meeting_id": meeting_id, "cluster_id": cluster_id, "max_seconds": secs},
                timeout=CLIP_TIMEOUT_S)


def clips_dir() -> Path:
    raw = os.environ.get(CLIPS_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_SOCKET.parent / "clips"


def read_clip(result: Dict[str, Any]) -> Optional[bytes]:
    """The clip's bytes, only from a regular ``.m4a`` file inside Minutes' clips directory and at most
    :data:`CLIP_MAX_BYTES`, and only an MP4 (an ``ftyp`` box first, as Prism requires); None otherwise.
    A path from the socket is never followed elsewhere."""
    try:
        path = Path(str(result.get("path") or "")).resolve(strict=True)
        root = clips_dir().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if root not in path.parents or path.suffix.lower() != ".m4a" or not path.is_file():
        return None
    try:
        if path.stat().st_size > CLIP_MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > CLIP_MAX_BYTES or data[4:8] != b"ftyp":
        return None
    return data


def describe(exc: MinutesError) -> str:
    """A plain-language reason for the owner."""
    plain = {
        "unavailable": "the Minutes app is not running on the agent host",
        "timeout": "the Minutes app did not answer",
        "busy": "Minutes is already recording something else",
        "not_ready": "Minutes is not ready",
        "permission_denied": "Minutes does not have permission to record the Mac's sound",
        "bad_response": "the Minutes app gave an answer Omni could not read",
    }.get(exc.code)
    if plain and exc.message and exc.code in ("not_ready", "permission_denied", "busy"):
        return "%s (%s)" % (plain, exc.message.rstrip("."))
    return plain or (exc.message.rstrip(".") or exc.code)
