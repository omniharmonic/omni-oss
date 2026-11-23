#!/usr/bin/env python3
"""Install the Omni plugins into Hermes — dry run unless --apply.

    python3 hermes/install.py                  # print what would happen
    python3 hermes/install.py --apply          # link plugins, enable them, render SOUL.md, set limits
    python3 hermes/install.py --plugins-only --apply
    python3 hermes/install.py --soul-only [--apply]     # regenerate SOUL.md from config/identity.yaml
    python3 hermes/install.py --memory-only [--apply]   # raise Hermes memory limits only
    python3 hermes/install.py --limits-only [--apply]   # lower Hermes concurrency ceilings only
    python3 hermes/install.py --profile dev    # a laptop dev Hermes: every gateway platform off

Which Hermes home: --hermes-home, else $HERMES_HOME, else ~/.hermes (~/.hermes-dev for --profile dev).
Every `hermes` command this script runs gets that HERMES_HOME.

What it does:
  1. Symlinks hermes/plugins/* (omni-bridge, omni-resource-guard, omni-core-patches, omni-meet) into
     <home>/plugins and adds each to `plugins.enabled` (a user plugin loads only if listed there).
     omni-bridge does nothing until OMNI_SERVICE_TOKEN is in <home>/.env (see README.md).
  2. Sets Hermes' timezone from config/identity.yaml (`owner.timezone`), and adds hermes/skills
     (Omni's skills: briefing, tasks, commitments, scheduling, meetings, proactive) to
     skills.external_dirs.
  3. Renders <home>/SOUL.md from config/identity.yaml (hermes/scripts/render_soul.py).
  4. Raises Hermes' memory limits (never lowers them; --memory-only) and lowers delegation
     concurrency (never raises it; --limits-only). config.yaml is backed up before the first change.

This registers no cron jobs. Routines (briefings, the hourly proactive pass, the memory mirror) are
installed separately and individually by scripts/install_proactive.py; see routines/README.md.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent



# Hermes gateway platforms (gateway/config.py Platform enum, minus `local`).
GATEWAY_PLATFORMS = (
    "telegram", "discord", "whatsapp", "whatsapp_cloud", "slack", "signal",
    "mattermost", "matrix", "email", "sms", "dingtalk", "api_server", "webhook",
    "msgraph_webhook", "feishu", "wecom", "wecom_callback", "weixin",
    "bluebubbles", "qqbot", "yuanbao", "relay",
)


def resolve_home(arg: str | None, profile: str, environ=os.environ) -> Path:
    if arg:
        return Path(arg).expanduser()
    if environ.get("HERMES_HOME", "").strip():
        return Path(environ["HERMES_HOME"]).expanduser()
    return Path.home() / (".hermes-dev" if profile == "dev" else ".hermes")


def hermes_cmd(home: Path, override: str | None) -> list:
    if override:
        return [override]
    # Hermes v0.23+ (PM): the published launcher; the old in-tree venv can no longer import Hermes.
    launcher = home / "hermes-agent/.hermes/bin/hermes"
    if launcher.is_file():
        return [str(launcher)]
    return [str(home / "hermes-agent/venv/bin/python"), "-m", "hermes_cli.main"]


def read_config(home: Path) -> dict:
    cfg = home / "config.yaml"
    if not cfg.exists():
        return {}
    import yaml
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def plan_enabled(cfg: dict, installed: list, wanted: list) -> list | None:
    """New `plugins.enabled` list, or None when nothing needs to change.

    Missing/malformed key: keep every installed plugin (what Hermes'
    grandfathering would enable) plus ours, so enabling the guard never
    silently disables another plugin.
    """
    plugins = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else {}
    current = plugins.get("enabled")
    if isinstance(current, list):
        missing = [w for w in wanted if w not in current]
        return (list(current) + missing) if missing else None
    out = []
    for name in list(installed) + list(wanted):
        if name not in out:
            out.append(name)
    return out


#: Backups already taken by this run, per home: one run backs up once, before its FIRST change, so a later step
#: never replaces the pristine copy with a half-changed one.
_BACKED_UP: dict = {}


def backup_config(home: Path) -> Path | None:
    cfg = home / "config.yaml"
    if not cfg.exists():
        return None
    if str(home) in _BACKED_UP:
        return _BACKED_UP[str(home)]
    dst = cfg.with_name(f"config.yaml.bak-{datetime.now():%Y%m%d-%H%M%S-%f}")  # %f: two steps in one run never share a name
    shutil.copy2(cfg, dst)
    _BACKED_UP[str(home)] = dst
    return dst


def yaml_list(items: list) -> str:
    return "[" + ", ".join(items) + "]"


def run(cmd, apply, env):
    print("  $", " ".join(cmd))
    if apply:
        subprocess.run(cmd, check=True, env=env)


def link_plugin(src: Path, dst: Path, apply: bool) -> None:
    if dst.is_symlink() and dst.resolve() == src.resolve():
        print(f"  ok   {dst} -> {src}")
        return
    if dst.exists() or dst.is_symlink():
        print(f"  WARN {dst} exists and is not a link to {src} — left alone")
        return
    print(f"  link {dst} -> {src}")
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)


def step_soul(home: Path, apply: bool) -> int:
    print("3. SOUL.md from config/identity.yaml")
    sys.path.insert(0, str(REPO / "hermes/scripts"))
    import render_soul
    render_soul.install(home, apply)
    return 0




#: Hermes built-in memory limits, in characters (tools/memory_tool.py MemoryStore).
MEMORY_LIMITS = (("memory.memory_char_limit", 8000), ("memory.user_char_limit", 4000))


def plan_memory_limits(cfg: dict) -> list:
    """(dotted key, value) pairs to set: each limit that is missing, not a number, or below its target.
    A limit already at or above the target is left alone (never lowered)."""
    memory = cfg.get("memory") if isinstance(cfg.get("memory"), dict) else {}
    out = []
    for key, target in MEMORY_LIMITS:
        current = memory.get(key.split(".", 1)[1])
        if isinstance(current, bool) or not isinstance(current, int) or current < target:
            out.append((key, target))
    return out


def step_memory(home: Path, hermes, env, apply: bool) -> int:
    print("3d. Hermes memory limits")
    changes = plan_memory_limits(read_config(home))
    if not changes:
        print("  ok   " + ", ".join("%s >= %d" % kv for kv in MEMORY_LIMITS))
        return 0
    if apply:
        b = backup_config(home)
        if b:
            print(f"  backup {b}")
    for key, value in changes:
        run(hermes + ["config", "set", key, str(value)], apply, env)
    return 0


#: Ceilings on how much Hermes may run at once (a research turn that fans out can exhaust a small
#: home server's memory). Only ever LOWERED, never raised. child_timeout_seconds is left
#: alone on purpose: upstream disabled it (heartbeat staleness catches stuck children; a stopwatch
#: killed legitimate long research). Browser sessions are capped by the omni-resource-guard plugin.
CONCURRENCY_CEILINGS = (("delegation.max_concurrent_children", 3),)


def plan_limits(cfg: dict) -> list:
    """(dotted key, value) pairs to set: each ceiling that is missing, not a number, or above its target."""
    out = []
    for key, target in CONCURRENCY_CEILINGS:
        section, name = key.split(".", 1)
        node = cfg.get(section) if isinstance(cfg.get(section), dict) else {}
        current = node.get(name)
        if isinstance(current, bool) or not isinstance(current, int) or current > target:
            out.append((key, target))
    return out


def step_limits(home: Path, hermes, env, apply: bool) -> int:
    print("3e. concurrency ceilings")
    changes = plan_limits(read_config(home))
    if not changes:
        print("  ok   " + ", ".join("%s <= %d" % kv for kv in CONCURRENCY_CEILINGS))
        return 0
    if apply:
        b = backup_config(home)
        if b:
            print(f"  backup {b}")
    for key, value in changes:
        run(hermes + ["config", "set", key, str(value)], apply, env)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--hermes-home", help="Hermes home (default $HERMES_HOME or ~/.hermes)")
    ap.add_argument("--hermes-bin", help="hermes executable")
    ap.add_argument("--profile", choices=("prod", "dev"), default="prod")
    ap.add_argument("--plugins-only", action="store_true", help="only step 1")
    ap.add_argument("--soul-only", action="store_true", help="only step 3")
    ap.add_argument("--memory-only", action="store_true", help="only step 4: raise Hermes memory limits")
    ap.add_argument("--limits-only", action="store_true", help="only step 4: lower Hermes concurrency ceilings")
    a = ap.parse_args(argv)

    home = resolve_home(a.hermes_home, a.profile)
    _BACKED_UP.clear()
    hermes = hermes_cmd(home, a.hermes_bin)
    env = dict(os.environ, HERMES_HOME=str(home))
    print(f"Hermes home: {home}  (profile: {a.profile})")

    if a.soul_only:
        return step_soul(home, a.apply)
    if a.memory_only:
        rc = step_memory(home, hermes, env, a.apply)
        if not a.apply:
            print("(dry run — nothing changed.)")
        return rc
    if a.limits_only:
        rc = step_limits(home, hermes, env, a.apply)
        if not a.apply:
            print("(dry run — nothing changed.)")
        return rc

    print("1. plugins")
    plugin_dir = home / "plugins"
    ours = sorted(p.name for p in (REPO / "hermes/plugins").iterdir() if p.is_dir())
    for name in ours:
        link_plugin(REPO / "hermes/plugins" / name, plugin_dir / name, a.apply)
    installed = sorted(p.name for p in plugin_dir.iterdir()) if plugin_dir.is_dir() else []
    new_list = plan_enabled(read_config(home), installed, ours)
    if new_list is None:
        print(f"  ok   plugins.enabled already lists {', '.join(ours)}")
    else:
        if a.apply:
            b = backup_config(home)
            if b:
                print(f"  backup {b}")
        run(hermes + ["config", "set", "plugins.enabled", yaml_list(new_list)], a.apply, env)
    if a.plugins_only:
        if not a.apply:
            print("(dry run — nothing changed.)")
        return 0

    sys.path.insert(0, str(REPO / "hermes/scripts"))
    import render_soul
    tz = (render_soul.load_identity().get("owner") or {}).get("timezone") or "UTC"
    print("2. timezone")
    run(hermes + ["config", "set", "timezone", tz], a.apply, env)
    print("2b. skills")
    run(hermes + ["config", "set", "skills.external_dirs", f"[{REPO / 'hermes/skills'}]"], a.apply, env)
    step_soul(home, a.apply)
    step_memory(home, hermes, env, a.apply)
    step_limits(home, hermes, env, a.apply)

    if a.profile == "dev":
        print("dev. gateway platforms off")
        for p in GATEWAY_PLATFORMS:
            run(hermes + ["config", "set", f"platforms.{p}.enabled", "false"], a.apply, env)

    print("\nRestart Hermes; with OMNI_SERVICE_TOKEN set, the gateway log shows 'omni-bridge: ACTIVE'.")
    if not a.apply:
        print("(dry run — nothing changed.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
