#!/usr/bin/env python3
"""
GWS Wrapper — gog-to-gws command mapper for future migration.

Maps gog CLI syntax to gws CLI syntax. Falls back to gog on failure.
NOT used by any scripts yet — future-proofing for when gws adds
multi-account support (--account flag).

Current limitation: gws v0.8.0 has single OAuth credential only.
The agent requires two accounts (owner@example.com +
agent@example.com), so gog remains primary.

Usage:
    python3 scripts/gws_wrapper.py gmail messages search "in:inbox" --account owner@example.com
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT_ROOT / "logs" / "gws_migration.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Command mapping: gog syntax → gws syntax
# ---------------------------------------------------------------------------

def map_gmail_search(gog_args: list[str]) -> list[str]:
    """Map: gog gmail messages search "query" → gws gmail search "query"."""
    query = next((a for a in gog_args if not a.startswith("--")), "")
    max_results = "10"
    for i, a in enumerate(gog_args):
        if a == "--max" and i + 1 < len(gog_args):
            max_results = gog_args[i + 1]
    return ["gws", "gmail", "search", query, "--max-results", max_results]


def map_gmail_get(gog_args: list[str]) -> list[str]:
    """Map: gog gmail get <id> → gws gmail get <id>."""
    msg_id = next((a for a in gog_args if not a.startswith("--")), "")
    return ["gws", "gmail", "get", msg_id]


def map_calendar_events(gog_args: list[str]) -> list[str]:
    """Map: gog calendar events primary --from X --to Y → gws calendar list --start X --end Y."""
    cmd = ["gws", "calendar", "list"]
    for i, a in enumerate(gog_args):
        if a == "--from" and i + 1 < len(gog_args):
            cmd.extend(["--start", gog_args[i + 1]])
        elif a == "--to" and i + 1 < len(gog_args):
            cmd.extend(["--end", gog_args[i + 1]])
    return cmd


GOG_TO_GWS = {
    ("gmail", "messages", "search"): map_gmail_search,
    ("gmail", "get"): map_gmail_get,
    ("calendar", "events"): map_calendar_events,
}


def strip_account_flag(args: list[str]) -> tuple[list[str], str | None]:
    """Remove --account flag (unsupported by gws) and return (clean_args, account)."""
    clean = []
    account = None
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a == "--account" and i + 1 < len(args):
            account = args[i + 1]
            skip_next = True
        else:
            clean.append(a)
    return clean, account


def find_mapper(args: list[str]):
    """Find the best matching gog→gws mapper for the given args."""
    for key_len in (3, 2):
        key = tuple(args[:key_len])
        if key in GOG_TO_GWS:
            return GOG_TO_GWS[key], args[key_len:]
    return None, args


def log_usage(cli_used: str, command: str, success: bool):
    """Log which CLI was used for migration tracking."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cli": cli_used,
        "command": command[:200],
        "success": success,
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run(args: list[str]) -> int:
    """Try gws first, fall back to gog."""
    clean_args, account = strip_account_flag(args)
    mapper, remaining = find_mapper(clean_args)

    if mapper:
        gws_cmd = mapper(remaining)
        logger.info(f"Trying gws: {' '.join(gws_cmd)}")
        try:
            result = subprocess.run(gws_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                print(result.stdout)
                log_usage("gws", " ".join(gws_cmd), True)
                return 0
            logger.warning(f"gws failed (exit {result.returncode}), falling back to gog")
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning(f"gws unavailable ({e}), falling back to gog")

    # Fallback: run original gog command
    gog_cmd = ["gog"] + args
    logger.info(f"Using gog: {' '.join(gog_cmd)}")
    result = subprocess.run(gog_cmd, capture_output=True, text=True, timeout=60)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    log_usage("gog", " ".join(gog_cmd), result.returncode == 0)
    return result.returncode


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
