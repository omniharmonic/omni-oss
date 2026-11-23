"""Make sure the standard macOS tool directories are on PATH.

Hermes v0.23's cron worker builds its own environment (cron/scheduler.py, build_subprocess_env) and
the PATH it hands no-agent scripts drops Homebrew (and may drop the system directories). Our jobs
shell out to `docker` (Matrix bridge health), `security` (the Proton Bridge password in the
Keychain) and `gog`. Without them, every Matrix-message and email event was held back as
"unverified" from 2026-11-12 until this fix.

Missing directories are APPENDED: whatever the caller put first still wins.
"""

from __future__ import annotations

import os
from typing import MutableMapping, Optional

STANDARD = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def ensure(environ: Optional[MutableMapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    parts = [p for p in (env.get("PATH") or "").split(os.pathsep) if p]
    env["PATH"] = os.pathsep.join(parts + [d for d in STANDARD if d not in parts])
    return env["PATH"]
