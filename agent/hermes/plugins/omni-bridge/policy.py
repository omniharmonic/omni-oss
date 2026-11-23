"""The Omni tool policy: full power, guarded edges.

`decide(tool, args, cwd)` answers, for one tool call of an Omni turn:

  allow    run it. A shell command runs inside the OS sandbox (sandbox.py) at
           the tier named here.
  block    refuse, with a message that names the legitimate way.
  approve  pause and ask the owner, in the Omni app, with this exact call.

The rules are the table in guard_table.py; the places are in paths.py. This
file only applies them. It never raises: a fault in here is a `block`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import guard_table as T
from . import contain, paths, redact, sandbox, shell

ALLOW, BLOCK, APPROVE = "allow", "block", "approve"

_INTERPRETERS = re.compile(r"^(python|pypy)[0-9.]*$|^(node|nodejs|deno|bun|ruby|perl|php|bash|sh|zsh|dash|osascript|lua|Rscript|swift|tsx|ts-node)$")
_WRITERS = frozenset({"cp", "mv", "rm", "rmdir", "ln", "tee", "touch", "mkdir", "install", "chmod", "chown", "chgrp", "truncate",
                      "dd", "sed", "perl", "ed", "ex", "vi", "vim", "nano", "emacs", "patch", "rsync", "tar", "unzip", "ditto",
                      "git", "plutil", "defaults", "sponge", "shred", "unlink", "trash", "mktemp", "curl", "wget"})
_SUBST_OK = frozenset({"date", "echo", "printf", "pwd", "whoami", "basename", "dirname", "hostname"})
_URL = re.compile(r"\b((?:https?|wss?|ftp|ftps|sftp|ssh|git|smtp|smtps|telnet|gopher|file)://[^\s'\"<>]+)", re.IGNORECASE)
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})
_STR_LIT = re.compile(r"""(?:"((?:[^"\\\n]|\\.){1,400})"|'((?:[^'\\\n]|\\.){1,400})')""")
_PATHISH = re.compile(r"^(~|\.{0,2}/|/)|/")
_OUTWARD_TOOL = re.compile(r"(^|_)(send|post|publish|tweet|email|mail|pay|transfer|invite)(_|$)", re.IGNORECASE)

_GRANTS = {
    "repo-env": lambda: [paths.repo_root() + "/.env"],
    "proton-state": lambda: [paths.repo_root() + "/state/proton-mail-state.json"],
    "gog": lambda: [paths.HOME() + "/Library/Application Support/gogcli", paths.HOME() + "/.config/gogcli"],
    "gh": lambda: [paths.HOME() + "/.config/gh"],
    "ssh": lambda: [paths.HOME() + "/.ssh"],
    "pm2": lambda: [paths.HOME() + "/.pm2"],
    "claude": lambda: [paths.HOME() + "/.claude/.credentials.json", paths.HOME() + "/.claude.json"],
}
# git: an https remote authenticates through gh's credential helper (`gh auth git-credential`), ssh through ~/.ssh.
_TRANSPORTS = {"git": ("ssh", "gh"),"ssh": ("ssh",), "scp": ("ssh",), "sftp": ("ssh",), "rsync": ("ssh",), "gh": ("gh",),
               "pm2": ("pm2",), "gog": ("gog",), "gws": ("gog",)}


@dataclass
class Decision:
    action: str
    #: Row id in guard_table (or a built-in: secret.path, self.files, write.outside, …).
    rule: str = ""
    title: str = ""
    #: To the model (block) / on the card (approve).
    message: str = ""
    # ── for the terminal: how it runs once allowed (or once approved) ──
    tier: str = "contained"
    network: bool = False
    keychain: bool = False
    secrets: Tuple[str, ...] = ()
    write_anywhere: bool = False
    #: The approved command IS a program that acts outside the sandbox (launchctl, osascript, open …).
    automation: bool = False
    #: The line starts a signed-out Chrome (agent-browser, Chrome, Playwright): sandbox.profile(browser=True).
    browser: bool = False
    #: Extra environment for the sandboxed command (never a credential).
    env: Dict[str, str] = field(default_factory=dict)
    #: Run from here (a trusted repo script always runs from the repo).
    cwd: Optional[str] = None
    #: The command to run (the request prefix, if any, removed).
    command: Optional[str] = None
    #: Extra facts for the card.
    detail: Dict[str, Any] = field(default_factory=dict)

    def trusted_env(self) -> Tuple[str, ...]:
        # Only the sole pinned read-only producer can use this loopback service credential.
        return ("OMNI_SERVICE_TOKEN",) if self.tier == "reader" and self.rule == "read.omni-nudge-sweep" else ()

    def grants(self) -> List[str]:
        out: List[str] = []
        for name in self.secrets:
            out += _GRANTS[name]() if name in _GRANTS else []
        return out


def _block(rule: str, title: str, message: str) -> Decision:
    return Decision(BLOCK, rule, title, "%s (omni-bridge: %s)" % (message, rule))


#: Rows whose approved command needs what the sandbox otherwise closes in every tier.
_AUTOMATION_RULES = frozenset({"sys.services", "sys.automation"})
#: curl / wget options that change WHERE a request really goes, or what it reads to decide.
_REROUTE = re.compile(r"^(-x|--proxy|--preproxy|--socks\w*|--resolve|--connect-to|--unix-socket|--abstract-unix-socket|"
                      r"-K|--config|--next|--doh-url|--dns-\S+|--interface|--netrc\S*|-T|--upload-file|-F|--form\S*|"
                      r"-d|--data\S*|--json|-b|--cookie\S*|-E|--cert|--key|-u|--user|-H|--header|-e|--referer|"
                      r"-i|--input-file|--post-\S+|--body-\S+|--execute|--use-askpass|-o|-O|--output\S*|--remote-name\S*)(=|$)")
#: Hermes tools the guard KNOWS and lets run without asking (their paths and addresses are
#: still checked). A tool that is on no list — a new Hermes release's, another plugin's —
#: asks first: nobody has yet looked at what it can do. OMNI_BRIDGE_TOOLS_FREE adds names.
KNOWN_FREE = frozenset({
    "omni_manage_proactivity",  # bounded schema; host verifies explicit owner intent
    "web_search", "web_extract", "x_search", "process", "read_file", "write_file", "patch", "search_files", "vision_analyze",
    "image_generate", "skills_list", "skill_view", "browser_navigate", "browser_snapshot", "browser_click", "browser_scroll",
    "browser_back", "browser_get_images", "browser_vision", "browser_console", "text_to_speech", "todo", "session_search",
    "clarify", "delegate_task", "ha_list_entities", "ha_get_state", "ha_list_services", "kanban_show", "kanban_list",
    "kanban_attachments", "tool_search", "tool_describe", "omni_propose",
    "omni_card",  # offers options on a card; sends nothing
    # Meetings: reading state, reading a transcript, and stepping back (leave / revoke) reach nobody.
    "omni_meet_status", "omni_meet_transcript", "omni_meet_cancel", "meet_status", "meet_transcript", "meet_leave",
})
_URL_TOOLS = frozenset({"web_extract", "web_search", "browser_navigate", "vision_analyze", "x_search", "image_generate"})
_WRITE_KEYS = contain.WRITE_KEYS
_TEXT_KEYS = contain.TEXT_KEYS


def allowed_hosts() -> frozenset:
    """Hosts the shell may fetch from without asking: OMNI_BRIDGE_EGRESS_ALLOW (default none)."""
    extra = [h.strip().lower() for h in os.environ.get("OMNI_BRIDGE_EGRESS_ALLOW", "").split(",") if h.strip()]
    return frozenset(extra)


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _port_of(url: str) -> Optional[int]:
    try:
        parts = urlsplit(url if "://" in url else "http://" + url)
        return parts.port or {"https": 443, "wss": 443}.get(parts.scheme, 80)
    except ValueError:
        return None


def _private_host(host: str) -> bool:
    """This machine, or an address on a private / link-local network (including the
    decimal, hex and IPv6 spellings of one), or a name with no dot in it."""
    host = (host or "").strip("[]").lower()
    if not host or host in ("localhost", "0", "broadcasthost") or host.endswith((".local", ".localhost", ".internal", ".lan", ".home.arpa", ".ts.net")):
        return True
    try:
        import ipaddress
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = ipaddress.ip_address(int(host, 0)) if re.match(r"^(0x[0-9a-f]+|\d+)$", host) else None
        if ip is not None:
            mapped = getattr(ip, "ipv4_mapped", None)
            ip = mapped or ip
            return not ip.is_global
    except ValueError:
        return True
    if re.match(r"^[0-9.]+$|^0x", host):
        return True                 # `127.1`, `0177.0.0.1`: short and octal forms of an address
    return "." not in host


def _repo_python(interp: str, cwd: Optional[str], repo: str) -> bool:
    """Is this the Python a trusted repo script may be run with: `python` / `python3` from
    PATH, or the repo's own `venv/bin/python*`?

    The venv's interpreter is judged by WHERE THE NAME IS, not by what it resolves to: in a
    venv `python` is a symlink (often a chain) that ends at the system's Python, outside
    the repo. The directory is resolved, the last name is not. (`venv/` is one of the
    guard's own paths: no Omni command can put something else there.)
    """
    if re.match(r"^python(3(\.\d+)?)?$", interp):
        return True
    if "/" not in interp or "\0" in interp:
        return False
    p = os.path.expanduser(interp)
    if not os.path.isabs(p):
        if not cwd:
            return False
        p = os.path.join(cwd, p)
    where = paths.real(os.path.dirname(p))
    return where == paths.real(repo + "/venv/bin") and bool(re.match(r"^python(3(\.\d+)?)?$", os.path.basename(p)))


def _script_of(cmd: shell.Simple) -> Optional[str]:
    """The script an interpreter runs (`python3 -u scripts/x.py a b` → `scripts/x.py`)."""
    if not cmd.argv or not _INTERPRETERS.match(cmd.prog):
        return None
    i = 1
    while i < len(cmd.argv):
        a = cmd.argv[i]
        if a in ("-m",):
            return "-m:" + (cmd.argv[i + 1] if i + 1 < len(cmd.argv) else "")
        if a in ("-c", "-e", "--eval", "-p", "--print", "-r", "-E"):
            return None
        if a in ("-W", "-X"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def _facts(cmd: shell.Simple) -> Tuple[str, Optional[str], str]:
    """(program, script, arguments-as-text) of one invocation."""
    script = _script_of(cmd)
    if script is not None and not script.startswith("-m:"):
        idx = cmd.argv.index(script)
        return cmd.prog, script, " ".join(cmd.argv[idx + 1:])
    return cmd.prog, script, " ".join(cmd.argv[1:])


def _matrix_read_args(cmd: shell.Simple) -> bool:
    """Only the exact bounded CLI read forms receive the Matrix credential."""
    script = _script_of(cmd)
    args = cmd.argv[cmd.argv.index(script) + 1:] if script else cmd.argv[1:]
    if not args:
        return False
    verb, rest = args[0], args[1:]
    _READ_VERBS = ("rooms", "recent", "read", "search", "find-room")
    # Work unblock 6: help needs no credential and changes nothing — for the read verbs only.
    if verb in ("--help", "-h") and not rest:
        return True
    if verb in _READ_VERBS and rest in (["--help"], ["-h"]):
        return True
    if verb == "read" and rest:
        # `read ROOM [--limit N]` in either order, `--limit=N` too; N up to 500.
        room, limit, i = None, None, 0
        while i < len(rest):
            a = rest[i]
            if a == "--limit" and i + 1 < len(rest):
                limit, i = rest[i + 1], i + 2
                continue
            if a.startswith("--limit="):
                limit, i = a.split("=", 1)[1], i + 1
                continue
            if a.startswith("-") or room is not None:
                return False
            room, i = a, i + 1
        if room is None or not 1 <= len(room) <= 256 or any(ch in room for ch in "\r\n\x00"):
            return False
        return limit is None or (limit.isdigit() and 1 <= int(limit) <= 500)
    if verb == "rooms":
        return not rest
    if verb == "recent":
        return not rest or (len(rest) == 2 and rest[0] == "--hours" and rest[1].isdigit() and 1 <= int(rest[1]) <= 168)
    if verb not in {"read", "search", "find-room"} or not rest or rest[0].startswith("-") or not 1 <= len(rest[0]) <= 256:
        return False
    if any(char in rest[0] for char in "\r\n\x00"):
        return False
    options = rest[1:]
    if verb == "search":
        return not options
    return not options or (len(options) == 2 and options[0] in {"--platform", "-p"} and options[1].lower() in {"matrix", "telegram", "whatsapp", "discord", "messenger", "instagram", "twitter", "signal"})


def _matches(rule: T.Rule, cmd: shell.Simple, repo_script_name: Optional[str]) -> bool:
    prog, script, argtext = _facts(cmd)
    args_re, unless_re, text_re = T.compiled(rule)
    if rule.id in _HELP_EXEMPT and cmd.code is None and cmd.prog in _HELP_CLIS and _asks_help(cmd.argv):
        return False                   # work unblock 9: help output prints no credential
    if rule.prog or rule.script:
        hit = False
        if rule.prog and prog in rule.prog:
            hit = True
        if rule.script:
            if rule.script == ("*repo-script*",):
                hit = hit or repo_script_name is not None
            elif script and shell.basename(script) in rule.script:
                hit = True
            elif prog in rule.script:          # run directly: ./scripts/x.py
                hit = True
        if not hit:
            return False
        if args_re and not args_re.search(argtext):
            return False
        if unless_re and unless_re.search(argtext):
            return False
        check = _GOG_NOTIFY_CHECKS.get(rule.id)
        if rule.id == "secret.browser-session":
            return _browser_signed_in(cmd)
        return check(cmd.argv) if check else True
    if text_re:
        # Text rows look at program TEXT — inline code, a here-document handed to an
        # interpreter — and at what a network program was told to reach. Never at plain
        # arguments of other programs (searching your notes for "smtplib" is not a send).
        if cmd.code is not None and cmd.lang not in (None, "text") and text_re.search(cmd.code):
            return True
        if prog in T.NETWORK_PROGS or prog in ("python", "python3", "node", "ruby", "perl", "php", "osascript") or \
                rule.id.startswith(("secret.", "net.python-server")):
            return bool(text_re.search(" ".join(cmd.argv)))
    return False


_HELP_EXEMPT = frozenset({"secret.token-mint", "secret.hermes-config"})
_HELP_CLIS = frozenset({"gog", "gws", "gh", "parachute", "op", "gcloud", "hermes"})


def _asks_help(argv: List[str]) -> bool:
    """`--help` anywhere, or `-h` as the LAST word (elsewhere it can be a value flag: `gh auth status -h HOST`)."""
    return "--help" in argv[1:] or (len(argv) > 1 and argv[-1] == "-h")


def _code_paths(code: str, cwd: Optional[str]) -> List[str]:
    """Paths written as string literals in inline code."""
    out = []
    for m in _STR_LIT.finditer(code or ""):
        lit = m.group(1) if m.group(1) is not None else m.group(2)
        if lit and _PATHISH.search(lit) and "\n" not in lit and len(lit) < 300:
            rp = _real(lit, cwd)
            if rp:
                out.append(rp)
    return out


def _real(piece: str, cwd: Optional[str]) -> Optional[str]:
    """Real path of a word; None for a relative path when the directory is unknown —
    except that a credential-looking NAME is judged wherever it would be."""
    expanded = os.path.expanduser(piece)
    if os.path.isabs(expanded):
        return paths.real(expanded)
    if cwd:
        return paths.real(expanded, cwd)
    while expanded.startswith("./"):
        expanded = expanded[2:]
    probe = "/__unknown__/" + expanded
    return probe if paths.is_secret(probe) else None


def _paths_of(cmd: shell.Simple, cwd: Optional[str]) -> Tuple[List[str], List[str]]:
    """(paths the command names, paths it would write by redirection)."""
    named: List[str] = []
    written: List[str] = []
    for a in cmd.argv[1:]:
        if not a or a.startswith("-") and "=" not in a:
            continue
        for piece in (a.split("=", 1)[1],) if (a.startswith("-") and "=" in a) else (a,):
            piece = piece[1:] if piece.startswith("@") else piece          # curl -d @file
            if "\0" in piece or "://" in piece:
                continue
            if _PATHISH.search(piece) or piece.startswith(".") or os.sep not in piece and (cwd and os.path.lexists(os.path.join(cwd, piece))):
                rp = _real(piece, cwd)
                if rp:
                    named.append(rp)
            elif paths.is_secret("/__name__/" + piece):
                # A bare file name that is a credential's by its name (`prism-server.db`, `id_rsa`).
                named.append("/__name__/" + piece)
    for op, target in cmd.redirects:
        if not target or "\0" in target or op in ("<<", ">&", "<&"):
            continue
        p = _real(target, cwd)
        if p:
            (written if op.startswith(">") or op in ("&>", "&>>", "<>") else named).append(p)
    if cmd.code and cmd.lang not in (None, "text"):
        named += _code_paths(cmd.code, cwd)
    return named, written


def _sensitive_mention(text: str) -> bool:
    low = text.lower()
    if re.search(r"[.]env\b|[.]ssh\b|keychain|[.]hermes|id_rsa|id_ed25519|credentials|[.]aws\b|[.]gnupg|[.]pm2|launchagents|private[_ ]?key|secret|token|passw", low):
        return True
    return any(d.lower() in low for d in paths.secret_dirs())


def internal_autonomy() -> bool:
    """Owner-controlled mode; an agent cannot edit the protected Hermes env."""
    return os.environ.get("OMNI_BRIDGE_AUTONOMY", "").strip().lower() == "internal"


def _autonomous(d: Decision, *, terminal: bool = False) -> Decision:
    if not internal_autonomy() or d.action == BLOCK or d.rule in T.OUTWARD_APPROVALS:
        return d
    # execute_code bypasses terminal containment; arbitrary code remains available
    # through the terminal instead, where the outbound credential boundary holds.
    if d.rule == "tool.execute-code":
        return _block("tool.execute-code", "Run code through the terminal",
                      "Write your script and run it with terminal. Internal work needs no approval there; execute_code cannot enforce the outbound approval boundary.")
    d.action = ALLOW
    if terminal:
        # Keep trusted readers' exact credential grants and environment contract.
        if d.tier != "reader":
            d.tier = "approved"
            d.network = True
            d.write_anywhere = True
    elif d.rule == "write.outside":
        d.write_anywhere = True
    return d


def decide_shell(command: str, cwd: Optional[str] = None) -> Decision:
    return _autonomous(_decide_shell(command, cwd), terminal=True)


def _decide_shell(command: str, cwd: Optional[str] = None) -> Decision:
    """Route one shell command line."""
    if not isinstance(command, str) or not command.strip():
        return Decision(ALLOW, command=command)
    if not cwd or not os.path.isdir(cwd):
        cwd = None
    # An explicit request from the model: `OMNI_NEEDS=network <command>` asks the owner up front.
    asked = None
    m = re.match(r"^\s*OMNI_NEEDS=([A-Za-z,_-]+)\s+", command)
    if m:
        asked = m.group(1)
        command = command[m.end():]
    parsed = shell.parse(command)
    cmds = [c for c in parsed.commands if c.argv]
    lead = parsed.cd or _lead_cd(parsed)
    eff_cwd = paths.real(lead, cwd) if lead else cwd
    repo = paths.repo_root()
    # Where each command would run: a plain `cd <dir>` earlier on the line moves the ones
    # after it (`cd ws && python3 x.py && echo ok >> log.txt`). A `cd` whose target is only
    # known at run time leaves the directory unknown — relative paths are then not guessed.
    where: Dict[int, Optional[str]] = {}
    cur: Optional[str] = cwd
    for c in cmds:
        if c.via:
            where[id(c)] = cur
            continue
        if c.prog == "cd":
            target = c.argv[1] if len(c.argv) > 1 else "~"
            cur = None if (c.dynamic or "\0" in target or target == "-") else paths.real(target, cur)
        where[id(c)] = cur

    def repo_script(cmd: shell.Simple) -> Optional[str]:
        """Name of the agent-repo script this runs, if it is one."""
        script = _script_of(cmd)
        cand = script if script and not script.startswith("-m:") else (cmd.argv[0] if "/" in cmd.argv[0] else None)
        if not cand:
            return None
        p = paths.real(cand, eff_cwd)
        return os.path.basename(p) if p.startswith(repo + "/scripts/") else None

    # 1 ── refusals: the guard itself, sends, secrets. Checked on every invocation the
    #      line would make, however it is wrapped.
    for cmd in cmds:
        rs = repo_script(cmd)
        for rule in T.SELF + T.SENDS + T.SECRETS:
            if _matches(rule, cmd, rs):
                return _block(rule.id, rule.title, rule.why)
        named, written = _paths_of(cmd, where.get(id(cmd), eff_cwd))
        for p in named + written:
            if paths.is_secret(p):
                return _block("secret.path", "Read a credential or a private store",
                              "That path holds credentials or private history; it is not readable from an Omni conversation.")
        for p in written:
            if paths.is_guard(p):
                return _self_files_block(p)
        if cmd.prog in _WRITERS or cmd.prog in ("python", "python3"):
            for p in named:
                if paths.is_guard(p) and not (cmd.prog == "git" and not re.search(r"\b(config|hooks?|checkout|reset|clean|apply|am|mv|rm)\b", " ".join(cmd.argv))):
                    if cmd.prog in _WRITERS:
                        return _self_files_block(p)
    if redact.contains(command):
        return _block("secret.value", "A command that carries a credential's value",
                      "This command contains the value of a credential. It will not be run.")

    # Matrix's CLI also contains sends and state-changing operations. Invalid or
    # operator-only verbs must not fall through to autonomous generic scripts.
    for cmd in cmds:
        if repo_script(cmd) == "matrix_reader.py" and not _matrix_read_args(cmd):
            return _block("read.matrix-arguments", "Use a bounded Matrix reader",
                          "Use standalone matrix_reader.py rooms, recent --hours 1..168, read ROOM --limit 1..500, search QUERY, find-room NAME --platform PLATFORM, or --help. "
                          "health is an operator-only Docker probe; triage and auto-join change state. Send exact messages with omni_propose (kind message).")

    # 2 ── a trusted reader: the whole line is one known read-only command.
    real_cmds = [c for c in cmds if not c.via.startswith("$( )")]
    subst_cmds = [c for c in cmds if c.via.startswith("$( )")]
    grant = _grant_head(parsed, cwd)
    piped = grant is not None and not (parsed.standalone and len(real_cmds) == (2 if parsed.cd else 1))
    if grant is not None and all(c.prog in _SUBST_OK and not c.redirects and _exec_trusted(c.argv[0], cwd) for c in subst_cmds):
        cmd = grant[0]
        rs = repo_script(cmd)
        swapped = _gog_cred_swapped(cmd)
        for rule in T.READERS:
            if not _matches(rule, cmd, rs):
                continue
            if swapped and "gog" in rule.secrets:
                continue               # round 5: no gog reader under another home / token / client
            if rule.id == "read.matrix" and not _matrix_read_args(cmd):
                continue
            if rule.id == "read.gog-any" and (not _gog_read_only(cmd.argv) or _gog_foreign_cred(cmd.argv) or _gog_env_wrapped(cmd)):
                continue
            run_from = None
            if rule.repo_script:
                interp = cmd.argv[0]
                interp_ok = _repo_python(interp, eff_cwd, repo) or paths.real(interp, eff_cwd) == paths.real(repo + "/scripts/" + (rs or ""))
                if rs is None or not interp_ok:
                    continue           # a script of that name somewhere else is just a script
                if piped and _grant_head(parsed, cwd, keep_cwd=False) is None:
                    continue           # it runs from the repo: a relative output path would land there
                if piped and rule.id == "read.omni-nudge-sweep":
                    continue           # fix round 1 (C1): the loopback service token never reaches another stage
                run_from = repo
            return Decision(ALLOW, rule.id, rule.title, tier="reader", network=rule.network, keychain=rule.keychain,
                            secrets=rule.secrets, cwd=run_from, command=command if run_from is None else _strip_cd(command, grant[1]))

    # 3 ── needs the owner: egress, installs, services, destruction, other agents, repo scripts that act.
    need: Optional[Tuple[str, str, str]] = None
    need_rule: Optional[T.Rule] = None
    for cmd in cmds:
        rs = repo_script(cmd)
        for rule in T.APPROVALS:
            if not _matches(rule, cmd, rs):
                continue
            if rule.id == "net.program" and _only_local(cmd):
                continue
            if rule.id == "sys.rm-recursive" and _all_writable(cmd, where.get(id(cmd), eff_cwd)):
                continue
            need = (rule.id, rule.title, rule.why)
            need_rule = rule
            break
        if need:
            break
    # A browser started some other way (`npx playwright …`, `python -m playwright`) is the same row.
    if need is None and _browser_line(cmds):
        rule = next(r for r in T.APPROVALS if r.id == "sys.browser")
        need = (rule.id, rule.title, rule.why)
    # Any other WHOLE-command gog / gws that is not a read is a Google write: it gets gog's
    # credential below, so each newly granted OAuth scope (Forms, Slides …) works with no edit here.
    if need is None and grant is not None and not subst_cmds and _gog_any_write(grant[0]):
        rule = next(r for r in T.APPROVALS if r.id == "sys.google-write")
        need = (rule.id, rule.title, rule.why)
    if need is None:
        for cmd in cmds:
            here = where.get(id(cmd), eff_cwd)
            _named, written = _paths_of(cmd, here)
            # With no known directory a relative target is not guessed at: the sandbox decides.
            outside = [p for p in written if not paths.writable(p)] if here else []
            if outside:
                need = ("write.outside", "Write outside the workspace",
                        "Writes a file outside the workspace (%s)." % ", ".join(sorted(set(outside))[:3]))
                break
    if need is None and asked:
        need = ("ask.explicit", "The agent asked for more than the sandbox gives (%s)" % asked,
                "The agent says this command needs: %s." % asked)
    if need is None and parsed.opaque and _sensitive_mention(command):
        need = ("opaque.sensitive", "A command that could not be fully read and names a sensitive place",
                "Could not be fully read (%s) and it names a sensitive place." % "; ".join(parsed.opaque[:2]))
    if need is not None:
        d = Decision(APPROVE, need[0], need[1], need[2], tier="approved", network=True, write_anywhere=True, command=command,
                     automation=need[0] in _AUTOMATION_RULES or any(c.prog == "open" for c in cmds))
        if _browser_line(cmds) or (asked and "browser" in asked.lower().split(",")):
            d.browser = True
            d.env = dict(BROWSER_ENV)
        # A transport, or a trusted program, gets its own credential once the owner approved it —
        # and only when it is the whole command.
        if grant is not None and not subst_cmds and not (repo_script(grant[0]) and piped and _grant_head(parsed, cwd, keep_cwd=False) is None):
            cmd = grant[0]
            grants = list(_TRANSPORTS.get(cmd.prog, ()))
            if _gog_cred_swapped(cmd):
                grants = [g for g in grants if g != "gog"]
            if repo_script(cmd):
                # A row that names its own grant (sys.calendar-holds: gog only, never the repo .env) keeps it narrow.
                grants += list(need_rule.secrets) if need_rule is not None and need_rule.secrets else ["repo-env", "gog"]
                d.cwd = repo
                d.command = _strip_cd(command, grant[1])
            if grants:
                d.secrets = tuple(dict.fromkeys(grants))
                d.keychain = True
        d.detail = {"cwd": eff_cwd or cwd or "", "unread": list(parsed.opaque[:3])}
        return d

    # 4 ── everything else: run it, contained.
    net = False
    if parsed.standalone and len(real_cmds) == 1 and not subst_cmds and not parsed.opaque:
        cmd = real_cmds[0]
        net = _plain_fetch(cmd) and all(h in allowed_hosts() for h in _hosts(cmd))
    return Decision(ALLOW, "", "", tier="contained", network=net, command=command)


# ── a reader piped into filters, or with its output saved (work unblock 4, audit 3.5) ──
#
# A trusted reader's grant (gog's token, the repo's .env) is per SANDBOX, so every stage of the line
# holds it. A line keeps the grant only when every later stage is a filter that cannot open a file:
# no file operand, no option that names one, no program text that reads, writes or runs anything.
# The output is what the reader would have returned to the model anyway.

def _opts(argv: List[str], with_value: str, long_with_value: Sequence[str] = ()) -> Optional[List[str]]:
    """The operands of a filter (non-option words), or None for a form that cannot be read safely.
    `with_value`: short options that take a value; `long_with_value`: long ones (`--x v` / `--x=v`)."""
    out: List[str] = []
    i, rest = 1, argv
    while i < len(rest):
        a = rest[i]
        if a == "--":
            out += rest[i + 1:]
            break
        if a.startswith("--"):
            # Fix round 2 (N2): a long option NEVER takes the next word. BSD tools give several of them
            # an OPTIONAL `=value` (`grep --context[=num]`, `--color[=when]`), so the next word may be an
            # operand; counted as one, the form is refused (`--opt=value` is the way to pass a value).
            i += 1
            continue
        if re.match(r"^-\d+$", a):
            i += 1                                     # `head -50`
            continue
        if a.startswith("-") and len(a) > 1:
            for j, ch in enumerate(a[1:], start=1):
                if ch in with_value:
                    if j == len(a) - 1:
                        i += 1                     # value is the next word
                    break
            i += 1
            continue
        out.append(a)
        i += 1
    return out


_SED_SCRIPT = re.compile(r"^\s*((\d+|\$)(,(\d+|\$))?|/[^/\n]*/(,/[^/\n]*/)?)?\s*[pdq=]?\s*$|"
                         r"^\s*((\d+|\$)(,(\d+|\$))?\s*)?s(?P<d>[/|#,:])((?!(?P=d)).)*(?P=d)((?!(?P=d)).)*(?P=d)[gpiI0-9]*\s*$")
_AWK_BAD = re.compile(r"getline|system|close|fflush|[|>]|@load|@include|ENVIRON|PROCINFO|ARGV|ARGC|nextfile|FILENAME", re.IGNORECASE)
#: Fix round 1 (C1): the only filters a CREDENTIALED stream may pass through (every `_grant_head` grant is
#: one). awk and cat are not on it: a missed awk construct could open the granted token store.
_GRANT_FILTERS = frozenset({"head", "tail", "wc", "sort", "uniq", "cut", "tr", "column", "grep", "egrep", "fgrep", "sed", "jq"})
#: Fix round 1 (C1): jq builtins that read the environment, a file name or a module, whatever the input.
_JQ_BAD = re.compile(r"\b(import|include|env|input_filename|getpath|modulemeta|get_search_list|get_prog_origin|get_jq_origin)\b"
                     r"|\$ENV\b|\$__\w+", re.IGNORECASE)
# `input` / `inputs` stay allowed: with no file operand (enforced below) they read standard input only.


def _pure_filter(c: shell.Simple) -> bool:
    """A filter stage that cannot open a file or run anything (see the section comment)."""
    if c.dynamic or c.assigns or c.code is not None or c.stdin_script or not c.argv:
        return False
    a = c.argv
    prog = c.prog
    flags = [x for x in a[1:] if x.startswith("-")]
    if prog in ("head", "tail"):
        return _opts(a, "nc") == []
    if prog == "wc":
        return not any(x.startswith("--files0") for x in flags) and _opts(a, "") == []
    if prog == "sort":
        if any(x.split("=", 1)[0] in ("-o", "--output", "-T", "--temporary-directory", "--files0-from", "--random-source",
                                       "--compress-program") or re.match(r"^-[a-zA-Z]*[oT]", x) for x in flags):
            return False
        return _opts(a, "kt", ("--key", "--field-separator", "--buffer-size", "-S", "--parallel")) == []
    if prog == "uniq":
        return _opts(a, "fs", ("--skip-fields", "--skip-chars", "--check-chars")) == []
    if prog == "cut":
        return _opts(a, "bcdf", ("--bytes", "--characters", "--delimiter", "--fields", "--output-delimiter")) == []
    if prog == "tr":
        ops = _opts(a, "")
        return ops is not None and 1 <= len(ops) <= 2
    if prog == "column":
        return _opts(a, "csoRNHlWEdT", ("--separator", "--output-separator", "--table-columns", "--output-width")) == []
    if prog == "cat":
        return _opts(a, "") == []
    if prog == "grep" or prog == "egrep" or prog == "fgrep":
        if any(re.match(r"^-[a-zA-Z]*[frRd]", x) or x.split("=", 1)[0] in ("--file", "--recursive", "--dereference-recursive",
                                                                            "--directories", "--devices", "--include", "--exclude",
                                                                            "--exclude-from", "--exclude-dir") for x in flags):
            return False
        has_e = any(re.match(r"^-[a-zA-Z]*e", x) or x.startswith("--regexp") for x in flags)
        ops = _opts(a, "emABCD", ("--regexp", "--max-count", "--after-context", "--before-context", "--context", "--label"))
        return ops is not None and len(ops) == (0 if has_e else 1)
    if prog == "sed":
        if any(re.match(r"^-[a-zA-Z]*[ifs]", x) and not re.match(r"^-[nEru]+$", x) or
               x.split("=", 1)[0] in ("--in-place", "--file", "--separate", "--follow-symlinks", "-I") for x in flags):
            return False
        scripts = [a[i + 1] for i, x in enumerate(a[:-1]) if x in ("-e", "--expression")]
        ops = _opts(a, "e", ("--expression",))
        if ops is None:
            return False
        if not scripts:
            if len(ops) != 1:
                return False
            scripts = ops
        elif ops:
            return False
        return all(_SED_SCRIPT.match(x) for part in scripts for x in part.split(";") if x.strip())
    if prog in ("awk", "gawk", "mawk"):
        if any(x.split("=", 1)[0] in ("-f", "--file", "-i", "--include", "-l", "--load", "-E", "--exec") or
               re.match(r"^-[a-zA-Z]*f", x) for x in flags):
            return False
        ops = _opts(a, "vF", ("--assign", "--field-separator"))
        return ops is not None and len(ops) == 1 and not _AWK_BAD.search(ops[0])
    if prog == "jq":
        words: List[str] = []
        i = 1
        while i < len(a):
            x = a[i]
            name = x.split("=", 1)[0]
            if name in ("-f", "--from-file", "--rawfile", "--slurpfile", "--args", "--jsonargs", "-L", "--library-path",
                        "--seq-file") or re.match(r"^-[a-zA-Z]*[fL]", x) and not x.startswith("--"):
                return False
            if x in ("--arg", "--argjson"):
                i += 3                                 # --arg NAME VALUE: values, not files
                continue
            if x in ("--indent",):
                i += 2
                continue
            if x.startswith("-") and x != "-":
                i += 1
                continue
            words.append(x)
            i += 1
        # one program (no module import), no file operands
        return len(words) <= 1 and not (words and _JQ_BAD.search(words[0]))
    return False


_OUT_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>"})


def _lead_cd(parsed: shell.Parsed) -> Optional[str]:
    """The target of a line's leading plain `cd <dir> &&` (or `;`), also when more follows than one command."""
    st, seps = parsed.stages, parsed.separators
    if len(st) >= 2 and len(st[0]) == 1 and st[0][0].prog == "cd" and len(st[0][0].argv) == 2 and not st[0][0].redirects \
            and not st[0][0].dynamic and seps[:1] and seps[0] in ("&&", ";", "\n") and "\0" not in st[0][0].argv[1] \
            and st[0][0].argv[1] != "-":
        return st[0][0].argv[1]
    return None


def _exec_trusted(argv0: str, cwd: Optional[str]) -> bool:
    """Fix round 2 (N1/N3): may a program that holds a credential grant be THIS one? A name with a `/`
    must be an unplantable file (root-owned all the way up, or a guard path) and so must what it
    resolves to; a bare name must resolve, on the PATH a credentialed command really gets
    (paths.trusted_path, which sandbox.wrap uses), to such a file. `./head`, `/tmp/x/jq`, `/x/gog`,
    or a `gog` planted earlier on Hermes' PATH all fail."""
    import shutil
    if not argv0 or "\0" in argv0:
        return False
    if "/" in argv0:
        p = os.path.expanduser(argv0)
        if not os.path.isabs(p):
            if not cwd:
                return False
            p = os.path.join(cwd, p)
        literal = os.path.join(paths.real(os.path.dirname(p)), os.path.basename(p))
        return os.path.isfile(literal) and paths.unplantable(literal) and paths.unplantable(paths.real(literal))
    hit = shutil.which(argv0, path=paths.trusted_path())
    return bool(hit) and paths.unplantable(hit) and paths.unplantable(paths.real(hit))


def _grant_head(parsed: shell.Parsed, cwd: Optional[str], keep_cwd: bool = True) -> Optional[Tuple[shell.Simple, bool]]:
    """The command whose grant a line may keep, and whether the line starts `cd <dir> &&`: the line is
    `[cd D &&] HEAD [| FILTER]… [> path]`, every filter a _pure_filter, every redirect (`>`, `>>`,
    `2>&1` …) on the last stage only and into a writable place (a relative target only when the
    directory is known), no input redirection anywhere. None otherwise."""
    if parsed.opaque:
        return None
    stages = [st for st in parsed.stages]
    seps = list(parsed.separators)
    while seps and seps[-1] == "\n":
        seps.pop()
    if "(" in seps or ")" in seps or len(seps) != len(stages) - 1 or not stages:
        return None
    # `set -e` / `set -o pipefail` before the command changes only how the shell reports failure.
    while len(stages) >= 2 and len(stages[0]) == 1 and stages[0][0].prog == "set" and not stages[0][0].redirects and \
            all(re.match(r"^([-+][euxo]+|pipefail)$", x) for x in stages[0][0].argv[1:]) and seps[0] in ("&&", ";", "\n"):
        stages, seps = stages[1:], seps[1:]
    lead_cd = False
    if len(stages) >= 2 and len(stages[0]) == 1 and stages[0][0].prog == "cd" and len(stages[0][0].argv) == 2 \
            and not stages[0][0].redirects and not stages[0][0].dynamic and seps[0] in ("&&", ";", "\n"):
        lead_cd = True
        target = stages[0][0].argv[1]
        cwd = paths.real(target, cwd) if "\0" not in target and target != "-" else None
        stages, seps = stages[1:], seps[1:]
    if any(sep != "|" for sep in seps) or any(len(st) != 1 for st in stages):
        return None
    head = stages[0][0]
    if head.assigns or head.dynamic or head.code is not None or head.stdin_script:
        return None
    # Fix round 2 (N1/N3): every program of the line runs with the grant — the reader, any wrapper it
    # sits in (`timeout`, `nice` …; `env` can change PATH, so it never keeps a grant), and each filter.
    raws = parsed.raws[len(parsed.raws) - len(stages):] if len(parsed.raws) >= len(stages) else []
    if len(raws) != len(stages):
        return None
    for raw, st in zip(raws, stages):
        if raw.assigns or raw.prog == "env" or "env" in [x.strip().split(" ")[0] for x in st[0].via.split("→")]:
            return None
        if not _exec_trusted(raw.argv[0], cwd) or not _exec_trusted(st[0].argv[0], cwd):
            return None
    for st in stages[1:]:
        if st[0].prog not in _GRANT_FILTERS or not _pure_filter(st[0]):
            return None
    last = len(stages) - 1
    for n, st in enumerate(stages):
        c = st[0]
        outs = 0
        for op, target in c.redirects:
            if op in (">&",) and target in ("1", "2"):
                continue                               # 2>&1
            if op in _OUT_REDIRECTS and target == "/dev/null":
                continue                               # 2>/dev/null, on any stage
            if op not in _OUT_REDIRECTS or n != last:
                return None
            outs += 1
            if not target or "\0" in target or target.startswith("&"):
                return None
            if target == "/dev/null":
                continue
            if paths.real(target, cwd).startswith(("/dev/", "/private/dev/")) or target.startswith("/dev/"):
                return None                            # fix round 1 (M2): /dev/tcp, /dev/udp, /dev/fd …
            relative = not os.path.isabs(os.path.expanduser(target))
            if relative and (cwd is None or not keep_cwd):
                return None
            p = paths.real(target, cwd)
            if not paths.writable(p) or paths.is_secret(p) or paths.is_guard(p):
                return None
        if outs > 1:
            return None
    return head, lead_cd


#: Chrome cannot use its own sandbox inside this one; agent-browser reads its launch flags from here.
#: The crash reporter is off so Chrome does not need the owner's crash database where it can avoid it.
BROWSER_ENV = {"AGENT_BROWSER_ARGS": "--no-sandbox,--disable-crash-reporter,--disable-breakpad"}
_NPX = frozenset({"npx", "bunx", "pnpx"})


def _browser_line(cmds: Sequence[shell.Simple]) -> bool:
    """Does the line start Chrome: a browser program (guard_table.BROWSER_PROGS), `npx|bunx playwright
    …`, or `python -m playwright`? A node / python script that drives one says so itself with
    `OMNI_NEEDS=browser`."""
    for c in cmds:
        if c.prog in T.BROWSER_PROGS:
            return True
        if c.prog in _NPX or (c.prog in ("pnpm", "yarn") and c.argv[1:2] in (["dlx"], ["exec"])):
            words = [a for a in c.argv[1:] if not a.startswith("-") and a not in ("dlx", "exec")]
            if words and re.match(r"^(@playwright/[\w.-]+|playwright(-core)?|agent-browser)(@\S*)?$", words[0]):
                return True
        if _script_of(c) in ("-m:playwright",):
            return True
    return False


#: agent-browser flags / subcommands / environment that would act with the owner's own sessions.
_AB_ATTACH_FLAGS = frozenset({"--auto-connect", "--cdp", "--restore", "--session-name", "-p", "--provider", "--restore-save"})
_AB_PATH_FLAGS = frozenset({"--profile", "--state", "--config"})
_AB_ENV = re.compile(r"^AGENT_BROWSER_(AUTO_CONNECT|CDP|PROFILE|STATE|SESSION_NAME|RESTORE|PROVIDER|CONFIG|ENCRYPTION_KEY)$")


def _browser_signed_in(cmd: shell.Simple) -> bool:
    """An agent-browser invocation that would use a signed-in browser: attach to a running Chrome
    (`--auto-connect`, `--cdp`, `connect`), a profile / state / config file outside the workspace
    (a bare profile name is one of the owner's Chrome profiles), saved sign-ins (`--restore`,
    `--session-name`, `auth`), a cloud provider, or any of those set through the environment."""
    if any(_AB_ENV.match(k) for k in cmd.assigns) or "env" in [p.strip().split(" ")[0] for p in cmd.via.split("→")]:
        return True
    argv = cmd.argv[1:]
    words: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        name, eq, val = a.partition("=")
        if name in _AB_ATTACH_FLAGS:
            return True
        if name in _AB_PATH_FLAGS:
            value = val if eq else (argv[i + 1] if i + 1 < len(argv) else "")
            if _ab_foreign_path(value):
                return True
            i += 1 if eq else 2
            continue
        if not a.startswith("-"):
            words.append(a.lower())
        i += 1
    if words[:1] and words[0] in ("connect", "auth"):
        return True
    if words[:2] == ["state", "load"] and (len(words) < 3 or _ab_foreign_path(words[2])):
        return True
    return False


def _ab_foreign_path(value: str) -> bool:
    """A profile / state / config value that is not the agent's own: empty, a bare name (one of
    Chrome's profiles: `Default`, `Profile 1`), or an absolute / home path outside the workspace and
    scratch places. A relative path is left to the sandbox (The owner's Chrome directory is unreadable)."""
    if not value or "\0" in value:
        return True
    if value.startswith(("/", "~")):
        return not paths.writable(paths.real(value))
    return "/" not in value and not value.startswith(".")


def _plain_fetch(cmd: shell.Simple) -> bool:
    """`curl <url>` and nothing clever: a GET of named hosts, printed. No option that sends a
    file or a header, re-routes the request, or reads its targets from somewhere else."""
    if cmd.prog not in ("curl", "wget") or cmd.dynamic or cmd.redirects or not _hosts(cmd):
        return False
    for a in cmd.argv[1:]:
        if "\0" in a or _REROUTE.match(a) or (a.startswith("-") and not a.startswith("--") and re.search(r"[xKTFdbEuHeioO]", a[1:])):
            return False
        if not a.startswith("-") and not _URL.fullmatch(a) and not re.match(r"^[A-Za-z0-9.-]+(:\d+)?(/\S*)?$", a):
            return False
    return True


def _strip_cd(command: str, lead_cd: bool) -> str:
    """`cd <repo> && cmd` → `cmd` (the wrapper sets the directory itself)."""
    if not lead_cd:
        return command
    m = re.match(r"^\s*cd\s+(\"[^\"]*\"|'[^']*'|\S+)\s*(&&|;|\n)\s*", command)
    return command[m.end():] if m else command


def _hosts(cmd: shell.Simple) -> List[str]:
    hosts = []
    for a in cmd.argv[1:]:
        for u in _URL.findall(a):
            h = _host_of(u)
            if h:
                hosts.append(h)
    if not hosts and cmd.prog in ("curl", "wget", "http", "https", "xh"):
        # A bare host (`curl example.com/x`).
        for a in cmd.argv[1:]:
            if a.startswith("-") or "\0" in a:
                continue
            m = re.match(r"^([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(:\d+)?(/|$)", a)
            if m and ("." in m.group(1) or m.group(1) in _LOOPBACK):
                hosts.append(m.group(1).lower())
    return hosts


def _only_local(cmd: shell.Simple) -> bool:
    """A network program that does not need asking about: one that makes no connection
    (`openssl dgst`), a plain fetch from an allow-listed host, or a call to a port on this
    machine that OMNI_BRIDGE_LOOPBACK_PORTS opens."""
    if cmd.dynamic:
        return False
    if cmd.prog == "openssl":
        return not re.search(r"\b(s_client|s_server|ocsp|s_time)\b", " ".join(cmd.argv))
    if cmd.prog == "lsof":
        return True
    if cmd.prog not in ("curl", "wget", "http", "https", "xh", "nc", "ncat", "telnet", "websocat"):
        return False
    if cmd.prog in ("curl", "wget") and _plain_fetch(cmd) and all(h in allowed_hosts() for h in _hosts(cmd)):
        return True
    # this machine: every target must be loopback, on an opened port
    open_ports = set(sandbox.loopback_ports())
    if not open_ports:
        return False
    if cmd.prog in ("nc", "ncat", "telnet"):
        words = [a for a in cmd.argv[1:] if not a.startswith("-")]
        return len(words) == 2 and words[0].lower() in _LOOPBACK and words[1].isdigit() and int(words[1]) in open_ports
    if any(re.match(r"^(-x|--proxy|--preproxy|--socks\w*|--resolve|--connect-to|--unix-socket|--abstract-unix-socket|-K|--config|--doh-url)(=|$)", a)
           for a in cmd.argv[1:]):
        return False
    targets = []
    for a in cmd.argv[1:]:
        targets += _URL.findall(a)
    if not targets:
        targets = [a for a in cmd.argv[1:] if not a.startswith("-") and re.match(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(/|$)", a)]
    return bool(targets) and all(_host_of(t if "://" in t else "http://" + t) in _LOOPBACK - {"0.0.0.0"} and _port_of(t) in open_ports
                                 for t in targets)


def _all_writable(cmd: shell.Simple, cwd: Optional[str]) -> bool:
    """`rm -r` whose every target is inside the workspace or a scratch directory."""
    targets = [a for a in cmd.argv[1:] if not a.startswith("-")]
    if not targets or cmd.dynamic:
        return False
    for t in targets:
        if "\0" in t or any(ch in t for ch in "*?[") and not paths.writable(paths.real(os.path.dirname(t) or ".", cwd)):
            return False
        p = paths.real(t, cwd)
        if not paths.writable(p) or p in paths.workspace_roots() or p in paths.scratch_roots():
            return False
    return True


# ── Hermes' own tools ────────────────────────────────────────────────────────

def _tool_rule(name: str, args: Dict[str, Any]) -> Optional[T.ToolRule]:
    free = frozenset(x.strip() for x in os.environ.get("OMNI_BRIDGE_FREE", "").split(",") if x.strip())
    for rule in T.TOOLS:
        hit = any(name == t or (t.endswith("*") and name.startswith(t[:-1])) for t in rule.tools)
        if not hit:
            continue
        if rule.when_key:
            v = str(args.get(rule.when_key, "") or "").strip().lower()
            if rule.when and v not in rule.when:
                continue
            if rule.unless and v in rule.unless:
                continue
        if rule.can_free and rule.action == APPROVE and (rule.id in free or rule.id.split(".", 1)[-1] in free):
            continue
        return rule
    return None


def tools_free() -> frozenset:
    return frozenset(x.strip() for x in os.environ.get("OMNI_BRIDGE_TOOLS_FREE", "").split(",")
                     if x.strip() and not _OUTWARD_TOOL.search(x.strip()) and _tool_rule(x.strip(), {}) is None
                     and x.strip() not in ("terminal", "execute_code"))


def mcp_servers() -> frozenset:
    raw = os.environ.get("OMNI_BRIDGE_MCP_SERVERS", "") or "parachute,prism"
    return frozenset(re.sub(r"[^A-Za-z0-9_]", "_", s.strip()) for s in raw.split(",") if s.strip())


def _mcp_parts(tool_name: str) -> Optional[Tuple[str, str]]:
    if tool_name.startswith("mcp__"):
        rest = tool_name[len("mcp__"):]
        server, _, tool = rest.partition("__")
        return server, (tool or rest)
    if tool_name.startswith("mcp_"):
        # Hermes' own form, `mcp_<server>_<tool>`: the server is whichever configured one prefixes it
        # (longest first, so `my_vault` wins over `my`). No configured match: no server can be trusted.
        for server in paths.mcp_server_names():
            prefix = "mcp_%s_" % server
            if tool_name.startswith(prefix) and len(tool_name) > len(prefix):
                return server, tool_name[len(prefix):]
        return "", tool_name[len("mcp_"):]
    return None


def mcp_outward(tool: str) -> bool:
    """An MCP tool name that sends or publishes (both turn kinds). A name that starts with a read verb
    is a read — `get_email`, `list_payment_intents`, `get_post_comments` — unless a later segment is a
    send verb (`get_and_send_report`); every other name keeps the substring rule (`create_payout`)."""
    if re.search(T.MCP_READ_LEAD, tool, re.IGNORECASE):
        later = re.sub(T.MCP_READ_LEAD, "", tool, count=1, flags=re.IGNORECASE)
        return bool(re.search(T.MCP_SEND_SEGMENT, "_" + later, re.IGNORECASE)) and not re.match(r"^(posts?|shares?|invites?)([-_]|$)", later, re.IGNORECASE) \
            and not re.search(r"(^|[-_])(post|share)[-_](comments?|likes?|stats?|count|links?)([-_]|$)", later, re.IGNORECASE)
    return bool(re.search(T.MCP_OUTWARD, tool, re.IGNORECASE))


def mcp_connector_write(server: str, tool: str) -> Optional[Decision]:
    """A write through a connector that is not one of Omni's own stores (Canva `create_design`,
    ClickUp `create_task`, ...), on an Omni turn. Already decided before this is asked: the tool is
    not a read, not outward (`mcp_outward` blocked it), not destructive (approval already asked).
    `server` is "" when the name matched no server configured in Hermes.

    Return None to let the call run, or a Decision to hold it (APPROVE = an approval card).
    """
    # Owner policy (2026-11-16): internal work is free; only publishing/sharing is gated. A comment or
    # reply on a connector's shared item notifies its collaborators, so it counts as sharing.
    if mcp_connector_notifies(tool):
        r = T.MCP_RULES[2]
        return Decision(APPROVE, r[0], "A comment or reply through a connector", "Comments notify the item's collaborators.")
    return None


def mcp_connector_notifies(tool: str) -> bool:
    """A connector tool that posts a comment or reply, which reaches the item's collaborators."""
    return bool(re.search(r"(^|[-_])(comment|reply|replies|mention)([-_]|$)", tool, re.IGNORECASE)) \
        and not re.search(r"(^|[-_])(get|list|search|read|query|find|fetch|view|show|count|describe)[-_]", tool, re.IGNORECASE)


def _patch_targets(args: Dict[str, Any]) -> List[str]:
    out = [args[k] for k in ("path", "file_path", "filepath", "target") if isinstance(args.get(k), str) and args.get(k)]
    body = args.get("patch")
    if isinstance(body, str):
        out += re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", body, re.MULTILINE)
        out += re.findall(r"^\*\*\* Move to: (.+)$", body, re.MULTILINE)
    return [p.strip() for p in out]


def _read_targets(args: Dict[str, Any]) -> List[str]:
    out = []
    for k in ("path", "file_path", "filepath", "directory", "dir", "paths", "files", "target"):
        v = args.get(k)
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(item, str) and item:
                out.append(item)
    return out


_strings = contain.strings


def _cron_creation_origin() -> Optional[Dict[str, Any]]:
    """Match the context used by Hermes to persist a new job's origin."""
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM")
        chat = get_session_env("HERMES_SESSION_CHAT_ID")
        return {"platform": platform, "chat_id": chat} if platform and chat else None
    except Exception:
        return None


def _cron_stored_job(job_id: str) -> Optional[Dict[str, Any]]:
    # Lazy: the standalone policy checker does not require Hermes' cron runtime.
    from cron.jobs import get_job
    return get_job(job_id)


def _cron_raw_runner(args: Dict[str, Any]) -> bool:
    # Hermes' advertised monitor string maps non-HTTP values to monitor_script.
    # Reject unknown/object shapes too, rather than treating them as safe URLs.
    monitor = args.get("monitor")
    unsafe_monitor = monitor not in (None, "") and not (
        isinstance(monitor, str) and monitor.strip().lower().startswith(("http://", "https://")))
    return bool(args.get("script") or args.get("monitor_script") or
                args.get("no_agent") not in (None, False) or unsafe_monitor)


def _cron_guarded_origin(origin: Any) -> bool:
    return (isinstance(origin, dict) and origin.get("platform") == "api_server" and
            isinstance(origin.get("chat_id"), str) and
            re.fullmatch(r"omni_[0-9a-f]{8,64}|omni-(routine|proactive)-[A-Za-z0-9_-]{1,100}", origin["chat_id"]) is not None)


def _cron_deliver_ok(deliver: Any, origin: Any) -> bool:
    """Work unblock 7: a job may deliver only to the owner himself — `local`, `origin` when the origin
    is his guarded Omni thread, his Telegram home channel (`telegram`) or chat
    (`telegram:<OMNI_OWNER_TELEGRAM_CHAT>[:topic]`, owner_target), or a comma list of those. Absent / empty means
    Hermes' default, `origin`. Anything else (`all`, another chat, another platform) is refused."""
    if isinstance(deliver, (list, tuple)):
        deliver = ",".join(str(p).strip() for p in deliver if str(p).strip())
    if deliver is None or (isinstance(deliver, str) and not deliver.strip()):
        deliver = "origin"
    if not isinstance(deliver, str):
        return False
    for part in (p.strip() for p in deliver.split(",")):
        if part == "local":
            continue
        if part == "origin":
            if not _cron_guarded_origin(origin):
                return False
            continue
        if not owner_target(part):
            return False
    return True


def _cron_boundary(name: str, args: Dict[str, Any]) -> Optional[Decision]:
    if name not in ("cronjob", "cronjob_manage"):
        return None
    action = str(args.get("action") or "").strip().lower()
    mutating = action in ("create", "update", "resume", "run", "run_now", "trigger")
    if not mutating:
        return None
    if action == "create" or "deliver" in args:
        origin = _cron_creation_origin() if action == "create" else None
        if action != "create":
            try:
                job = _cron_stored_job(args.get("job_id")) if isinstance(args.get("job_id"), str) and args.get("job_id") else None
            except Exception:
                job = None
            origin = job.get("origin") if isinstance(job, dict) else None
        if not _cron_deliver_ok(args.get("deliver"), origin):
            return _block("cron.delivery", "Scheduled delivery to someone other than the owner",
                          "A job may deliver only to the owner: deliver='local', 'origin' (this Omni thread), 'telegram' "
                          "(his own chat) or 'telegram:%s'. Anything for other people is drafted with omni_propose by the job." % OWNER_TELEGRAM_CHAT)
    if _cron_raw_runner(args):
        return _block("cron.runner", "Raw scheduled runners bypass the agent guard",
                      "Use a local prompt job without script, monitor_script or no_agent. Its agent can use normal tools under the outbound policy.")
    if action == "create":
        origin = _cron_creation_origin()
        if not _cron_guarded_origin(origin):
            return _block("cron.origin", "A scheduled job must retain Omni's guard",
                          "Create the local prompt job from an Omni turn. This context would not store the api_server origin needed to guard later runs.")
        return None
    job_id = args.get("job_id")
    try:
        job = _cron_stored_job(job_id) if isinstance(job_id, str) and job_id else None
    except Exception:
        job = None
    if not isinstance(job, dict):
        return _block("cron.job-unavailable", "The scheduled job could not be verified",
                      "List jobs and retry with the exact job_id. No job runs or changes when its stored boundary cannot be checked.")
    origin = job.get("origin")
    if not _cron_deliver_ok(job.get("deliver"), origin):
        return _block("cron.delivery", "This job delivers to someone other than the owner",
                      "This job cannot run, resume or update from Omni. Create a new job that delivers to the owner "
                      "(local, origin, telegram or telegram:%s) instead." % OWNER_TELEGRAM_CHAT)
    if action != "update":
        # Resuming or running a job re-runs exactly what the owner set up (its script and prompt are not
        # the turn's to change here): a turn that may pause a job may undo the pause. Changing what an
        # unguarded job does (update) still needs the checks below.
        return None
    if not _cron_guarded_origin(origin):
        return _block("cron.origin", "This job does not retain Omni's guard",
                      "Create a new local prompt job from Omni; this stored job lacks a guarded api_server origin.")
    if _cron_raw_runner(job):
        return _block("cron.runner", "This job uses an unguarded runner",
                      "Create a local prompt job without script, monitor_script or no_agent instead.")
    return None


#: Tools that reach the web with something the turn chose (a URL, a query, a page to open).
_FETCH_NAME = re.compile(r"(^|_)(fetch|browse|browser|crawl|scrape|http|curl|wget|url)(_|$)", re.IGNORECASE)


def _setting_on(value) -> bool:
    """A yes/no setting: absent, False or a 'false'-like string is off; anything else (fail closed) is on."""
    if value is None or value is False:
        return False
    return not (isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0", ""))


def _setting_present(value) -> bool:
    """A value setting (URL, id, key): absent or blank is unset; anything else (fail closed) is set."""
    return not (value is None or (isinstance(value, str) and not value.strip()))


def real_profile_enabled() -> bool:
    """Can Hermes' browser act as a signed-in the owner? True for any of: `browser.use_real_profile`; a `browser.cdp_url`
    or `BROWSER_CDP_URL` (attaches to an existing, possibly signed-in Chrome); camofox persistence or identity
    (`browser.camofox.managed_persistence`, `.user_id`, `.session_key`). Config is `$HERMES_HOME/config.yaml`, read
    through Hermes (`hermes_cli.config.read_raw_config`) when it is there, else the file with PyYAML. Fails closed: a
    config that cannot be read, or a `browser` / `camofox` section that is not a mapping, counts as signed in."""
    try:
        if _setting_present(os.environ.get("BROWSER_CDP_URL")):
            return True
        try:
            from hermes_cli.config import read_raw_config
        except ImportError:
            import yaml
            with open(os.path.join(paths.hermes_home(), "config.yaml"), encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            cfg = {} if cfg is None else cfg
        else:
            cfg = read_raw_config()
            if cfg == {} and not _clean_without_browser(os.path.join(paths.hermes_home(), "config.yaml")):
                return True                    # an empty answer for a file that is missing, unreadable or says more
        if not isinstance(cfg, dict):
            return True
        browser = cfg.get("browser")
        if browser is None:
            return False                       # Hermes' default: a separate, signed-out profile
        if not isinstance(browser, dict):
            return True
        if _setting_on(browser.get("use_real_profile")) or _setting_present(browser.get("cdp_url")):
            return True
        camofox = browser.get("camofox")
        if camofox is None:
            return False
        if not isinstance(camofox, dict):
            return True
        return (_setting_on(camofox.get("managed_persistence"))
                or _setting_present(camofox.get("user_id")) or _setting_present(camofox.get("session_key")))
    except Exception:
        return True


def _clean_without_browser(path: str) -> bool:
    """Does Hermes' config file exist and read cleanly as empty, or as a mapping with no `browser` section? Only
    then is the reader's empty answer believed. Missing, unopenable, unparsable or anything else: no."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if not text.strip():
            return True
        import yaml
        parsed = yaml.safe_load(text)
        return parsed is None or (isinstance(parsed, dict) and "browser" not in parsed)
    except Exception:
        return False


def reaches_web(name: str, args: Optional[Dict[str, Any]], d: Decision) -> bool:
    """Would this call reach the web? Web, browser and fetch tools, an image read from a web address, and a contained
    shell command that would get the network. A command the owner approves card by card stays his call. Hermes'
    tool-search bridge (`tool_call`) is judged as the tool it calls, as `_decide` does (`d` is already the inner
    call's decision)."""
    if name == "tool_call" and isinstance((args or {}).get("name"), str):
        return reaches_web(args["name"], _inner_args(args), d)
    if name in _URL_TOOLS or name.startswith(("web_", "browser_")) or _FETCH_NAME.search(name or ""):
        if name == "vision_analyze":
            return any(isinstance(u, str) and re.match(r"^\s*https?:", u, re.I) for _k, u in _strings(args or {}))
        return name != "image_generate"
    mcp = _mcp_parts(name or "")
    if mcp is not None and _FETCH_NAME.search(mcp[1]):
        return True
    return name == "terminal" and d.action == ALLOW and bool(d.network)


def card_egress(name: str, args: Optional[Dict[str, Any]], d: Decision) -> bool:
    """Is this call refused on an item card's thread? Those turns (and proactive runs) read untrusted email and chat
    text. Owner decision 2026-10-22: they browse like any Omni turn, in a signed-out browser, so nothing logged in
    is in reach. While Hermes' browser uses the owner's signed-in profile (`browser.use_real_profile`), a call that
    would reach the web is refused there again (`net.card-thread`).

    Narrowed 2026-11-17: only the BROWSER carries those sign-ins. Search, extract and fetch tools hold no session, and
    a shell cannot reach the browser (its DevTools port is closed in every sandbox tier, its profile is a secret
    directory, `agent-browser --cdp` is secret.browser-session). So on a card thread with a signed-in browser,
    `browser_*` calls are refused and everything else keeps the web."""
    return reaches_web(name, args, d) and _browser_tool(name, args) and real_profile_enabled()


def _browser_tool(name: str, args: Optional[Dict[str, Any]]) -> bool:
    if name == "tool_call" and isinstance((args or {}).get("name"), str):
        return _browser_tool(args["name"], _inner_args(args))
    return (name or "").startswith("browser_")


_URL_IN_TEXT = re.compile(r"\bhttps?://[^\s'\"<>`]+", re.IGNORECASE)


def egress_hosts(name: str, args: Optional[Dict[str, Any]]) -> List[str]:
    """The hosts of the web addresses a call names (no path, query or credentials), in order, once each: what a
    card-thread turn's egress log records so the weekly audit can list domains."""
    if name == "tool_call" and isinstance((args or {}).get("name"), str):
        return egress_hosts(args["name"], _inner_args(args))
    out = []
    for _k, value in _strings(args or {}):
        for url in _URL_IN_TEXT.findall(value):
            try:
                host = (urlsplit(url).hostname or "").lower()
            except ValueError:
                continue
            if host and host not in out:
                out.append(host)
    return out


def _inner_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """The arguments of the tool a `tool_call` bridges to: an object, or the JSON text Hermes may pass."""
    inner = args.get("arguments")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            inner = {}
    return inner if isinstance(inner, dict) else {}


#: How Omni changes its own repo (scripts/omni_ship.py): the production checkout is never edited in place.
SHIP_HINT = ("To change it, edit your own clone of the repo in the workspace, run its tests, commit, and ship it "
             "(skill omni-self-improve). It goes live by itself once the tests pass; only a change to the guard, "
             "the senders or credentials comes to the owner, as a pull request.")


def _self_files_block(p: str, title: str = "Change the guard's own files") -> Decision:
    """A write to a protected path. Repo files have a way through (the ship); the rest are the owner's to change."""
    if paths.in_roots(p, [paths.repo_root()]):
        return _block("self.files", title, "That file is in the production checkout, which no turn edits in place. " + SHIP_HINT)
    return _block("self.files", title,
                  "That file is Hermes' own settings or code, how the services start, or the guard. A turn cannot change "
                  "it. If it needs to change, propose the exact change to the owner (what, where, and why).")


def _skill_repo_block() -> Decision:
    return _block("self.skill-repo", "Change a skill that lives in the agent repo",
                  "That skill is loaded from the Omni repo (or linked into it), and the production checkout is never "
                  "edited in place. " + SHIP_HINT + " For a quick lesson in the meantime, use `memory`.")


def decide(tool: str, args: Optional[Dict[str, Any]], cwd: Optional[str] = None) -> Decision:
    """Route one tool call of an Omni turn. Never raises."""
    try:
        return _autonomous(_decide(tool or "", args if isinstance(args, dict) else {}, cwd), terminal=tool == "terminal")
    except Exception:                                   # a fault in the policy closes the gate
        return _block("internal", "The tool policy failed", "omni-bridge could not judge this call, so it was not run.")


def _decide(name: str, args: Dict[str, Any], cwd: Optional[str]) -> Decision:
    # Hermes' tool-search bridge: judge the tool it would call.
    if name == "tool_call" and isinstance(args.get("name"), str):
        inner = args.get("arguments")
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                inner = {}
        return _decide(args["name"], inner if isinstance(inner, dict) else {}, cwd)
    if name in ("tool_search", "tool_describe", "omni_propose", "omni_card"):
        return Decision(ALLOW)

    # A credential's VALUE in the arguments of any tool: it is on its way somewhere.
    if True:
        try:
            if redact.contains(json.dumps(args, ensure_ascii=False, default=str)):
                return _block("secret.value", "A call that carries a credential's value",
                              "This call contains the value of a credential. It will not be made.")
        except (TypeError, ValueError):
            pass

    cron_boundary = _cron_boundary(name, args)
    if cron_boundary is not None:
        return cron_boundary

    # Before the row table: tool.skill-manage is freed on Omni turns, this never is.
    if name == "skill_manage" and paths.skill_write_protected(args):
        return _skill_repo_block()

    if name == "terminal":
        wd = args.get("workdir") if isinstance(args.get("workdir"), str) and args.get("workdir") else cwd
        d = decide_shell(str(args.get("command") or ""), wd)
        if d.cwd is None and isinstance(args.get("workdir"), str):
            d.detail.setdefault("cwd", args.get("workdir"))
        return d

    if name in ("read_file", "search_files"):
        for p in _read_targets(args):
            if paths.is_secret(paths.real(p, cwd)):
                return _block("secret.path", "Read a credential or a private store",
                              "That path holds credentials or private history; it is not readable from an Omni conversation.")
        return Decision(ALLOW)

    if name in ("write_file", "patch"):
        targets = [paths.real(p, cwd) for p in _patch_targets(args)]
        if not targets:
            return Decision(APPROVE, "write.outside", "Write a file whose path could not be read",
                            "The file this would change could not be determined.")
        for p in targets:
            if paths.is_secret(p):
                return _block("secret.path", "Write a credential file", "Credential files are not written from an Omni conversation.")
            if paths.is_guard(p):
                return _self_files_block(p)
        outside = [p for p in targets if not paths.writable(p)]
        if outside:
            return Decision(APPROVE, "write.outside", "Write outside the workspace",
                            "Writes %s, outside the workspace (%s)." % (", ".join(outside[:3]), paths.default_workspace()),
                            detail={"paths": outside[:20]})
        return Decision(ALLOW)

    # A path handed to ANY other tool (an image to look at, a file to save, an MCP server's
    # argument) is held to the same rules as the file tools.
    is_mcp = _mcp_parts(name) is not None
    for key, value in _strings(args):
        if key in _TEXT_KEYS or not contain.is_local(value):
            continue
        p = paths.real(contain.local_part(value), cwd)
        if paths.is_secret(p):
            return _block("secret.path", "Read a credential or a private store",
                          "That path holds credentials or private history; it is not readable from an Omni conversation.")
        if key in _WRITE_KEYS:
            if paths.is_guard(p):
                return _self_files_block(p)
            if not paths.writable(p):
                return Decision(APPROVE, "write.outside", "Write outside the workspace",
                                "Writes %s, outside the workspace (%s)." % (p, paths.default_workspace()), detail={"paths": [p]})
        if is_mcp and os.path.lexists(p) and not paths.writable(p):
            # An MCP server is another program, outside the sandbox, that would open this path itself.
            # A workspace file is handed over as a private copy read inside the sandbox (work unblock 9,
            # __init__: contain.private_paths), so it cannot change under the server; elsewhere the owner decides.
            return Decision(APPROVE, "mcp.local-path", "Hand an MCP server a local file (%s)" % p[:200],
                            "This server would open a local file itself, outside the sandbox.", detail={"paths": [p]})

    mcp = _mcp_parts(name)
    if mcp is not None:
        server, tool = mcp
        trusted = bool(server) and server in mcp_servers()
        if mcp_outward(tool):
            r = T.MCP_RULES[0]
            return _block(r[0], r[2], r[3])
        if re.search(T.MCP_DESTRUCTIVE, tool, re.IGNORECASE) and not re.search(T.MCP_NOT_DESTRUCTIVE, tool, re.IGNORECASE):
            r = T.MCP_RULES[1]
            return Decision(APPROVE, r[0], r[2], r[3])
        if not trusted and not re.search(T.MCP_READ, tool, re.IGNORECASE) and not re.search(T.MCP_READ_LEAD, tool, re.IGNORECASE):
            held = mcp_connector_write(server, tool)
            if held is not None:
                return held
        return Decision(ALLOW)

    if name == "browser_console" and isinstance(args.get("expression"), str) and args["expression"].strip():
        name = "browser_exec"          # JavaScript run in the page is scripting the browser

    rule = _tool_rule(name, args)
    if rule is not None:
        if rule.action == BLOCK:
            return _block(rule.id, rule.title, rule.why)
        return Decision(APPROVE, rule.id, rule.title, rule.why)

    # What a tool would carry OUT in a URL or a query.
    if name in _URL_TOOLS or name.startswith(("web_", "browser_")):
        for v in args.values():
            for u in (v if isinstance(v, list) else [v]):
                if isinstance(u, str) and len(u) > 1024 and "://" in u[:12]:
                    return Decision(APPROVE, "net.url-payload", "Open a very long web address",
                                    "A web address this long can carry data out with it (%d characters)." % len(u))
        for _key, u in _strings(args):
            m = re.match(r"^\s*([A-Za-z][A-Za-z0-9+.-]*):", u)
            if not m or len(u) > 4096 or _key in _TEXT_KEYS:
                continue
            scheme = m.group(1).lower()
            if scheme in ("data", "file", "about", "blob"):
                continue                       # nothing leaves; a file path was judged above
            if scheme not in ("http", "https"):
                return Decision(APPROVE, "net.local-url", "Open an address that is not a web page (%s:)" % scheme,
                                "Not an http(s) address; it may run script or reach a local program.")
            if _private_host(_host_of(u.strip())):
                return Decision(APPROVE, "net.local-url", "Open an address on this machine or the home network",
                                "%s is this machine or a private network, not the public web. What listens there may act without asking who is calling."
                                % (_host_of(u.strip()) or "that address"))

    if _OUTWARD_TOOL.search(name) and name not in ("send_message",):
        return _block("tool.outward-name", "A tool that sends", "Sends go through an approval card. " + T.PROPOSE)
    # Known: on the free list, or a row of the table that did not apply this time (a read
    # action of a guarded tool, or a row the owner freed with OMNI_BRIDGE_FREE).
    in_table = any(name == t or (t.endswith("*") and name.startswith(t[:-1])) for row in T.TOOLS for t in row.tools)
    if name in KNOWN_FREE or in_table or name in tools_free():
        return Decision(ALLOW)
    return Decision(APPROVE, "tool.unknown", "A tool the guard has not seen before (%s)" % name[:60],
                    "omni-bridge has no rule for this tool, so it asks before the first use. "
                    "If it is safe to run freely, add its name to OMNI_BRIDGE_TOOLS_FREE.")


# ═════════════════════════════════════════════════════════════════════════════
# EVERY Hermes turn: the outward edge only (one door, owner requirement 2026-10-15)
#
# Nothing is sent to another person unless the owner approved that exact message in
# Omni. Omni turns get the whole policy above (sandbox, approval cards, readers).
# Every OTHER turn — the Telegram gateway, the desktop app, the CLI, cron jobs that
# did not come from Omni — gets only this: sends, credential dumps and switching the
# guard off are refused; everything else runs exactly as before. Those surfaces have
# no OS sandbox, so this is matching on what a call says it does (best effort); the
# hard stop is in the send scripts themselves (scripts/send_grant.py: no Prism grant,
# no send).
# ═════════════════════════════════════════════════════════════════════════════

#: The owner's own Telegram chat: Hermes may message HIM from any surface.
OWNER_TELEGRAM_CHAT = os.environ.get("OMNI_OWNER_TELEGRAM_CHAT", "").strip()  # config: The owner's own Telegram chat id; empty = none
#: Rows of the guard itself that a non-Omni turn must not use to switch the guard off.
_SELF_OUTWARD = frozenset({"self.launchd-env", "self.proton-owner-auth"})
_MSG_TOOLS = frozenset({"send_message", "react_to_message", "start_chat"})
#: Send scripts named in inline code / file contents (no argument check possible there).
_SEND_SCRIPT_NAMES = tuple(sorted({s for r in T.SENDS if r.script and not r.args for s in r.script}))
#: `gog`/`hermes` invocations and their verbs, as they appear tokenised in inline code
#: (`subprocess.run(['gog','send',…])`) or as a string (`os.system('gog send …')`). A
#: separator is a quote, comma, bracket or whitespace, so both forms match.
_SEP = r"[\"',\s\]\[]+"
_CODE_SENDS = re.compile(
    r"\b(%s)\b"
    r"|\b(gmail|mail|email)\b%s(send|reply|forward)\b"
    r"|\b(calendar|cal)\b%s(respond)\b"
    r"|\bchat\b%s[^\n]{0,40}?\bsend\b"
    r"|\bgog\b%s(send|reply|forward|respond)\b"
    r"|\bhermes\b%s(send|chat)\b"
    r"|matrix_reader\.py%s(send|send-to)\b"
    # Google APIs used directly (review round 3): Gmail send, Calendar events with attendees.
    r"|\.messages\(\)\s*\.send\s*\(|\bmessages\.send\b"
    # Review R4 I-b: a Gmail DRAFT send is a send.
    r"|\.drafts\(\)\s*\.send\s*\(|\bdrafts\.send\b"
    # Resume ruling: any Calendar event write from inline code (the body may be a variable holding attendees).
    r"|\.events\(\)\s*\.(insert|patch|update|import_|move|quickAdd)\s*\("
    r"|\bsendUpdates\s*=\s*['\"](all|externalOnly)"
    r"|google_api\.py%s(calendar%screate[^\n]{0,300}?attendees|drive%sshare)\b"
    % ("|".join(re.escape(x) for x in _SEND_SCRIPT_NAMES), _SEP, _SEP, _SEP, _SEP, _SEP, _SEP, _SEP, _SEP, _SEP),
    re.IGNORECASE)
#: Shell / code text that would let the agent decide FOR the owner: a decision on an Omni
#: approval, a Prism live action, or a Prism sign-in / device flow (B3, R2-1). Those need
#: his device; a turn never makes them. Matched on NORMALISED text (percent-decoding and
#: `/./`, `//`, `%2F` removed) so `decisio%6E`, `/./`, `//decision` and assembled-from-vars
#: forms do not slip through.
def _shell_flatten(text):
    """Undo the cheap shell disguises (review round 3, C1): empty quote pairs inside a word
    (`/au''th`, `a""uth`) and `$VAR` / `${VAR}` set by `VAR=value` on the same line
    (`P=8787; … 127.0.0.1:$P`). Best-effort: a value computed at run time is not seen."""
    t = text or ""
    try:
        t = re.sub(r"''|\"\"", "", t)
        assigned = dict(re.findall(r"(?:^|[;&|\s(])(?:export\s+)?([A-Za-z_]\w*)=([^\s;&|'\"`$()]*)", t))
        for k, v in assigned.items():
            t = re.sub(r"\$\{%s\}|\$%s(?!\w)" % (re.escape(k), re.escape(k)), lambda _m, v=v: v, t)
    except Exception:
        return text or ""
    return t


def _normalize_url_text(text):
    """Percent-decode (repeatedly) and canonicalise path separators so an obfuscated URL
    reads like the plain one. Never raises."""
    out = _shell_flatten(text or "")
    try:
        # Join split string literals ("'.e'+'nv'" -> ".env", "'deci'+'sion'" -> "decision")
        # so a URL or path assembled from fragments reads like the plain one.
        out = re.sub(r"['\"]\s*\+\s*['\"]", "", out)
        for _ in range(4):
            dec = re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), out)
            if dec == out:
                break
            out = dec
        out = out.replace("\\", "/")
        out = re.sub(r"/\./", "/", out)
        out = re.sub(r"/{2,}", "/", out)
    except Exception:
        return text or ""
    return out

def _decides_for_owner(text):
    """Text that would decide an approval or drive a Prism live action (needs the device)."""
    n = _normalize_url_text(text)
    if "/api/actions/" in n.lower():
        return True
    low = n.lower()
    return "approvals" in low and re.search(r"\b(decision|decide|send-grant)\b", low) is not None

#: A Prism sign-in / device / auth flow, or the Prism loopback origin, named off Omni (R2-1):
#: a turn must never drive the account's authentication from the shell or code.
#: Prism's own sign-in / device / password endpoints (M4: not any `/auth/` text).
_PRISM_AUTH = re.compile(r"/auth/(request|callback|set-password|change-password|device|devices|verify|magic|logout|login|invite|invite-info|register)\b", re.IGNORECASE)
#: Prism's origin on this machine: port 8787 on any loopback spelling, this host's own name
#: (`$(hostname)`, `$HOSTNAME`, `*.local`), or a numeric/hex loopback form (`127.1`,
#: `0x7f000001`). Port 8787 is Prism's here, so `wrangler dev` on localhost:8787 is refused
#: too off Omni (documented trade-off).
_PRISM_LOOPBACK = re.compile(
    r"(?<![\w.-])(127(?:\.\d{1,3}){0,3}|0x7f[0-9a-f]{0,6}|2130706433|localhost|\[?::1\]?|0\.0\.0\.0|"
    r"\$\(\s*hostname[^)]*\)|`\s*hostname[^`]*`|\$\{?HOSTNAME\}?|[\w-]+\.local):8787\b", re.IGNORECASE)
#: Review R4: Prism owns port 8787 on this machine, so off Omni ANY host on port 8787 is Prism
#: (the agent host's own LAN name/IP, `--resolve x:8787:…`), in any spelling of the port (leading
#: zeros, hex 0x2253), and a socket opened to a loopback address on 8787 in code.
#: Review R5 M-f: linear time. The old host prefix `[\w.\[\]:-]*` matched nothing the bare
#: `:port` does not (any run of host characters can be walked back to a non-word start), but
#: rescanned from every `.`/`:`/`-`/`[` — quadratic. Now: a literal `:`, a bounded run of
#: zeros, the port, no nested or unbounded quantifier and no `:` in a repeated class.
_PRISM_PORT = re.compile(r":(0{0,16}8787|0x0{0,16}2253)(?!\w)|"
                         r"[\"'](127(\.\d{1,3}){0,3}|localhost|::1|0\.0\.0\.0|[\w.-]{1,253})[\"']\s{0,16},\s{0,16}"
                         r"(0{0,16}8787|0x0{0,16}2253)\b", re.IGNORECASE)


#: Review R5 I-c: Google REST calls that reach another person, made with a raw token
#: (curl, requests, urllib …). Gmail send / draft send / import / insert / upload / batch.
_GOOGLE_GMAIL_REST = re.compile(
    r"googleapis\.com/(upload|batch)/gmail\b"
    r"|googleapis\.com/gmail/[^\s'\"]{0,300}?/(send|import|insert)\b"
    r"|gmail/v1/users/[^\s/'\"]{1,200}/(messages|drafts)/(send|import|insert)\b", re.IGNORECASE)
#: Calendar events (an attendee gets an invitation) and Drive permissions (a share mails the
#: grantee): refused when the call notifies, or writes.
_GOOGLE_NOTIFY_REST = re.compile(
    r"calendar/v3/calendars/[^\s'\"]{1,300}?/events\b|drive/v[23]/files/[^\s'\"]{1,300}?/permissions\b", re.IGNORECASE)
_GOOGLE_SEND_UPDATES = re.compile(r"send_?(updates|notifications|notification_?email)['\"]?\s{0,3}[=:]\s{0,3}['\"]?(all|externalonly|true)\b",
                                  re.IGNORECASE)
_HTTP_WRITE = re.compile(
    r"\b(post|patch|put|delete)\b|(?<!\S)(-[a-zA-Z]{0,8}[dFT]|--data[\w-]{0,12}|--json|--form[\w-]{0,8}|--upload-file|--method)(?![\w-])"
    r"|\b(data|json|body|content)\s{0,3}=|\burlopen\b|\brequest\s{0,3}\(|\bfetch\s{0,3}\(", re.IGNORECASE)


def _google_rest_send(text):
    """Text naming a Google REST call that sends mail, an invitation or a share (R5 I-c)."""
    n = _normalize_url_text(text)
    if _GOOGLE_GMAIL_REST.search(n):
        return True
    if _GOOGLE_NOTIFY_REST.search(n):
        return bool(_GOOGLE_SEND_UPDATES.search(n) or _HTTP_WRITE.search(n))
    return False


def _google_rest_block() -> Decision:
    return _send_block("send.google-rest", "A Gmail send or a Calendar / Drive write through Google's REST API",
                       "A Gmail send, a Calendar event write (attendees get an invitation) or a Drive share made "
                       "with a Google token reaches other people with no approval — and there is no server-side stop "
                       "for it. Draft it with omni_propose (kind email or calendar-invite).")


#: Review R5 I-c: google_api.py's send shapes, applied to ANY script an interpreter runs
#: (a copy, a symlink, a renamed file, `python3 - gmail send … < google_api.py`).
_GOOGLE_API_ARGS = re.compile(next(r for r in T.SENDS if r.id == "send.google-api").args, re.IGNORECASE)
_COPIERS = frozenset({"cp", "ln", "link", "mv", "rsync", "install", "ditto", "scp", "tar", "zip", "gzip", "cpio", "pax",
                      "dd", "tee", "base64", "xxd", "uuencode", "openssl", "gcp", "cat", "sed", "awk", "head", "tail",
                      "perl", "python", "python3"})


#: Review R6 I-d: gws's own send shapes (`gmail +send`, `users messages send`, `calendar
#: +insert`, `events insert`, `permissions create`), applied like _GOOGLE_API_ARGS to ANY
#: script an interpreter runs — so a renamed copy of gws_bridge.py is caught when it sends.
_GWS_SEND_ARGS = re.compile(r"(^|\s)\+(send|repl\w*|forw\w*|insert)\b|\b(messages|drafts)\s+send\b"
                            r"|\bevents\s+(insert|patch|update|import|move|quick-?add)\b|\bpermissions\s+create\b",
                            re.IGNORECASE)
#: The Google senders in Hermes' google-workspace skill: google_api.py, and gws_bridge.py,
#: which runs `gws <any args>` with the Hermes Google token (R6 I-d).
_GOOGLE_SENDER_NAMES = ("google_api", "gws_bridge")


def _gws_bridge_args(cmd: shell.Simple) -> Optional[List[str]]:
    """If `cmd` runs Hermes' `gws_bridge.py` (by interpreter, as `-m`, from stdin, or as the
    program itself), the arguments it passes on to `gws`; else None (R6 I-d)."""
    if not cmd.argv:
        return None
    if "gws_bridge" in cmd.prog.lower():
        return cmd.argv[1:]
    if not _INTERPRETERS.match(cmd.prog):
        return None
    script = _script_of(cmd)
    if script is not None and "gws_bridge" in (script[3:] if script.startswith("-m:") else shell.basename(script)).lower():
        idx = cmd.argv.index("-m") + 1 if script.startswith("-m:") else cmd.argv.index(script)
        return cmd.argv[idx + 1:]
    if any(op == "<" and "gws_bridge" in str(t).lower() for op, t in cmd.redirects):
        rest = cmd.argv[1:]
        return rest[rest.index("-") + 1:] if "-" in rest else rest
    return None


def _google_api_copy(cmd: shell.Simple, line: str) -> bool:
    """`cp|ln|cat >|tee … google_api.py …` (or gws_bridge.py): a copy of a sender, which
    could then be run under another name (R5 I-c, R6 I-d)."""
    names = [a for a in cmd.argv[1:] + [t for op, t in cmd.redirects if op == "<"]
             if any(n in str(a).lower() for n in _GOOGLE_SENDER_NAMES)]
    if not names:
        return False
    if cmd.prog in ("cp", "ln", "link", "mv", "rsync", "install", "ditto", "scp", "tar", "zip", "gzip", "cpio", "pax",
                    "dd", "tee", "base64", "xxd", "uuencode", "openssl", "gcp"):
        return True
    return cmd.prog in _COPIERS and any(op in (">", ">>", ">|", "&>", "&>>") for op, _t in cmd.redirects)


def _prism_control(text):
    n = _normalize_url_text(text)
    return bool(_PRISM_AUTH.search(n) or _PRISM_LOOPBACK.search(n) or _PRISM_PORT.search(n))
#: Credential places named inside code text (literal paths are also resolved below).
_CODE_SECRET = re.compile(r"prism-server[^\s'\"/]{0,255}\.db|(^|[/'\"\s=(])\.env(\.[\w-]+)?['\"\s)]|/\.env\b|Library/(Keychains|Messages|Mail)\b|"
                          r"/\.ssh/|proton-mail-state|/\.hermes/(\.env|auth\.json)|gogcli/|"
                          # Review R5 I-c: the Hermes Google OAuth token / client secret (gmail.send,
                          # calendar, drive scopes), however the name is assembled, and loading it.
                          r"google_token|client_secret|from_authorized_user_(file|info)|/\.hermes/[^\s'\"]{0,200}token", re.IGNORECASE)
#: A token / client-secret FILE name in code (`google_token.json`, `oauth-token.json`,
#: `client_secret_<id>….json`): each whole `[\w.-]` word ending `.json|.pickle|.pkl` is found
#: once (it must start a word, so the engine cannot restart inside it), then its stem is
#: checked for `token`/`client_secret` between `_ . -` separators. Linear time — the round-5
#: single pattern backtracked on a long word full of `_token_` (review R6 M-h).
_CODE_FILE_WORD = re.compile(r"(?<![\w.-])[\w.-]+\.(json|pickle|pkl)\b", re.IGNORECASE)
_TOKEN_STEM = re.compile(r"(^|[_.-])(tokens?|client_secrets?)([_.-]|$)", re.IGNORECASE)


def _code_secret_text(text: str) -> bool:
    if _CODE_SECRET.search(text):
        return True
    return any(_TOKEN_STEM.search(m.group(0)[:-len(m.group(1)) - 1]) for m in _CODE_FILE_WORD.finditer(text))


_APPLESCRIPT_SEND = re.compile(r"\bapplication\s*\(?\s*[\"']?(Mail|Messages|Microsoft Outlook|Outlook|Slack|Telegram|WhatsApp|Signal|Discord|Spark|Airmail)\b|\bsend\b",
                               re.IGNORECASE)
_HERMES_PROGS = frozenset({"hermes", "hermes-agent", "hermes-acp"})
#: gog / gws off Omni: READ subcommands only (B1). Aliases are normalised first.
_GOG_ALIASES = {"cal": "calendar", "mail": "gmail", "email": "gmail", "people": "contacts", "class": "classroom"}
_GOG_GENERIC_READS = frozenset({"list", "ls", "get", "info", "show", "search"})
_GOG_READS = {
    "calendar": {"events", "event", "list", "get", "show", "freebusy", "calendars", "colors", "search", "agenda"},
    "gmail": {"search", "get", "thread", "threads", "labels", "list", "attachment", "messages"},
    "drive": {"ls", "list", "search", "get", "info", "download", "export", "permissions", "comments"},
    "docs": {"cat", "text", "read", "get", "show", "info", "export", "list-tabs", "structure", "raw", "find-range", "comments"},
    "sheets": {"get", "metadata", "values"},
    "contacts": {"list", "search", "get", "me"},
    "tasks": {"list", "lists", "get"},
    "auth": {"list"},
    "forms": {"get", "info", "show", "raw", "responses", "watch"},
    "slides": {"get", "info", "show", "list", "ls", "export"},
}
_GOG_READS.update({g: set(_GOG_GENERIC_READS) for g in ("keep", "sites", "meet", "photos", "youtube", "analytics", "searchconsole",
                                                        "classroom", "groups", "appscript")})
_GOG_READS["chat"] = {"spaces", "messages", "threads", "dm"}
_GOG_THIRD = {("gmail", "messages"): {"list", "get", "search"}, ("sheets", "values"): {"get"},
              # work unblock 3: Chat reads (`chat spaces list`, `chat messages list S`) are reads
              ("chat", "spaces"): {"list", "ls", "get", "info", "show", "search", "find"},
              ("chat", "messages"): {"list", "ls", "get", "info", "show", "search", "find"},
              ("chat", "threads"): {"list", "ls", "get", "info", "show"}, ("chat", "dm"): {"list", "ls", "get", "info", "show"},
              ("forms", "responses"): {"list", "ls", "get", "info", "show"}, ("forms", "watch"): {"list", "ls"},
              ("docs", "comments"): set(_GOG_GENERIC_READS) - {"search"}, ("drive", "comments"): set(_GOG_GENERIC_READS) - {"search"}}
#: Top-level read shortcuts (`gog ls`, `gog me` …).
_GOG_TOP_READS = frozenset({"version", "help", "ls", "search", "me", "whoami", "status", "download"})
#: Words that refuse the generic credential on an Omni turn wherever they stand in the argv
#: (those commands keep only their own rows): Gmail and Chat (forwarding, vacation replies,
#: send-as, delegates and messages reach other people), the calendar (out-of-office and
#: focus-time auto-decline, propose-time notifies the organizer), gog's own credentials and
#: settings, long-lived servers, and any send / reply / forward / draft word (review fix 1).
_GOG_NO_GENERIC = re.compile(r"^(gmail|mail|email|chat|calendar|cal|auth|config|keyring|login|logout|credentials?|tokens?|accounts?|"
                             r"secrets?|mcp|serve|server|daemon|send|reply|forward|forwarding|drafts?|sendas|delegates|autoforward|"
                             r"vacation)$", re.IGNORECASE)
#: Global flags that point gog at another credential, client or home: a line that names one
#: anywhere is neither a generic reader nor a generic write (it fails closed). `--config` is
#: not a gog v0.25 flag; kept so it also fails closed after the group.
_GOG_FOREIGN_CRED_FLAGS = frozenset({"--client", "--home", "--access-token", "--config"})
#: gog's command-gating flags: their value names subcommands (`--enable-commands send,gmail`),
#: so a line that carries one is never classified as a read (review round 2, R2-2).
_GOG_GATE_FLAGS = frozenset({"--enable-commands", "--enable-commands-exact", "--disable-commands"})
_GOG_WRITE_WORDS = frozenset({"modify", "create", "delete", "update", "add", "remove", "trash", "untrash", "archive",
                              "send", "reply", "forward", "move", "rename", "patch", "insert", "import", "set", "clear",
                              # gog v0.25 aliases of those (work unblock 12)
                              "edit", "mv", "rm", "del", "new", "style", "color", "colour", "fwd"})
#: gog v0.25's global flags that take a value (so the value is not read as a subcommand) —
#: `gog --help` on the agent host, 2026-10-22. Any other flag before the subcommand fails closed.
_GOG_VALUE_FLAGS = frozenset({"--color", "--home", "-a", "--account", "--client", "--access-token", "--enable-commands",
                              "--enable-commands-exact", "--disable-commands", "--select"})
#: gog v0.25's global flags that take NO value (bare booleans). `-n` is --dry-run.
_GOG_BOOL_FLAGS = frozenset({"-h", "--help", "--gmail-no-send", "-j", "--json", "-p", "--plain", "--wrap-untrusted",
                             "--results-only", "-n", "--dry-run", "-y", "--force", "--no-input", "-v", "--verbose", "--version"})
_LOOKS_OUTWARD = re.compile(r"send|smtp|tweet|mail|telegram|matrix|post|publish|invite|respond|wallet|keychain|security\s", re.IGNORECASE)


def outward_guard_paths() -> List[str]:
    """Files whose change would switch the one door off: the guard plugin, Hermes' own
    settings and plugins, what starts the services, and the senders + their grant check.
    (Narrower than paths.guard_paths(): the desktop app and Telegram may still edit the
    rest of the repo and Hermes' skills/memories as before.)"""
    h, hh, repo = paths.HOME(), paths.hermes_home(), paths.repo_root()
    out = [repo + "/hermes", repo + "/.env", repo + "/launchd", hh + "/config.yaml", hh + "/.env", hh + "/plugins",
           h + "/.hermes/config.yaml", h + "/.hermes/.env", h + "/.hermes/plugins",
           h + "/Library/LaunchAgents", "/Library/LaunchAgents", "/Library/LaunchDaemons"]
    out += [repo + "/scripts/" + s for s in ("send_grant.py", "proton_send.py", "omni_tweet_send.py", "twitter_post.py",
                                             "matrix_reader.py", "send_telegram.py", "omni_propose.py", "agent_claude.py")]
    # Code the guard process itself imports or runs (omni-bridge __init__: proactive_intelligence.cards and
    # gen_capabilities rendering the capability manifest): editing it would rewrite the guard from outside.
    out += [repo + "/scripts/gen_capabilities.py", repo + "/config/omni-capabilities.yaml",
            repo + "/scripts/proactive_intelligence"]
    # Work unblock 11: the binaries that run with a credential (gog, gh, git, agent-browser …).
    out += paths.trusted_binaries()
    # A Python start-up hook in a trusted interpreter's site would run inside a credentialed run.
    out += paths.python_site_dirs()
    out += paths.python_prefixes()
    return sorted(set(paths.real(p) for p in out))


def _outward_guard(p: str) -> bool:
    return paths.in_roots(p, outward_guard_paths()) or p in paths.anchor_paths()


def _gog_read_only(argv: List[str]) -> bool:
    """`gog …` is one of the known READ forms (or asks for help / the version).

    A pre-subcommand flag must be `--x=y`, a known boolean, or a known value flag (which
    consumes its value). ANY OTHER flag before the subcommand makes the line not a known
    read — so `gog --select gmail --select search send …`, where an unknown flag's value
    would otherwise be mistaken for the subcommand (R2-2), is refused and falls to the
    SENDS rules / the gog.read-only block."""
    rest = argv[1:]
    if any(a in ("--help", "-h") for a in rest):
        return True
    words: List[str] = []
    skip = False
    for a in rest:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            if a.split("=", 1)[0] in _GOG_GATE_FLAGS:
                return False                   # names subcommands in its value: never a known read
            if "=" in a and a.startswith("--"):
                continue                       # --flag=value: self-contained
            if a in _GOG_BOOL_FLAGS:
                continue
            if a in _GOG_VALUE_FLAGS:
                skip = True
                continue
            return False                       # an unrecognised pre-subcommand flag: not a known read
        words.append(a)
        if len(words) == 3:
            break
    if not words:
        return False
    if words[0].lower() in _GOG_TOP_READS:
        return True
    group = _GOG_ALIASES.get(words[0].lower(), words[0].lower())
    if group == "classroom":
        return _gog_classroom_read(words)
    verbs = _GOG_READS.get(group)
    if not verbs or len(words) < 2 or words[1].lower() not in verbs:
        return False
    if (group, words[1].lower()) == ("drive", "permissions"):
        # `drive permissions F` / `permissions list F` list access; any other verb changes it (round 4).
        return len(words) < 3 or words[2].lower() not in (_GOG_WRITE_WORDS | _GRANT_VERBS | _GOG_PERM_CHANGE)
    third = _GOG_THIRD.get((group, words[1].lower()))
    if third is not None:
        return len(words) >= 3 and words[2].lower() in third
    if words[1].lower() in ("search", "find", "query"):
        return True                            # what follows is the query text, not a subcommand
    # `gmail thread modify`, `gmail labels create` …: a WRITE word in third place is not a read
    # (round 2 minor). They contact nobody, but off Omni every non-read gog form is refused.
    return not (len(words) >= 3 and words[2].lower() in _GOG_WRITE_WORDS)


#: Verbs that change a file's permissions beyond _GOG_WRITE_WORDS / _GRANT_VERBS.
_GOG_PERM_CHANGE = frozenset({"rm", "del", "share", "grant", "edit", "transfer"})


def _gog_classroom_read(words: List[str]) -> bool:
    """A classroom command is a read only when a read verb (list / get / search …) comes before
    any other verb in its first words; everything else is a write that notifies the class
    (controller ruling, round 4 — subgroup aliases are deliberately not enumerated)."""
    if len(words) < 2:
        return True                            # bare `gog classroom`: prints help
    if words[1].lower() in ("roster", "members"):
        # `roster (members) <courseId>` is a read-only leaf in v0.25 (round 5).
        return not any(x.lower() in _GOG_WRITE_WORDS or x.lower() in _GRANT_VERBS for x in words[2:4])
    for x in words[1:4]:
        x = x.lower()
        if x in _GOG_GENERIC_READS:
            return True
        if x in _GOG_WRITE_WORDS or x in _GRANT_VERBS:
            return False
    return False


def _gog_env_wrapped(cmd: shell.Simple) -> bool:
    """`env [-i] VAR=… gog …` (or `/usr/bin/env`): env can point gog at another home, token,
    client or keyring (GOG_HOME, GOG_ACCESS_TOKEN …), so it gets no grant — the same as the
    inline `VAR=… gog …` form, which is never standalone (round 4)."""
    return bool(cmd.assigns) or any(p.strip().split(" ")[0] == "env" for p in cmd.via.split("→"))


def _gog_cred_swapped(cmd: shell.Simple) -> bool:
    """A gog / gws command that is env-wrapped or names a foreign-credential flag: it gets NO
    gog grant on any path — no reader row, no generic write, no transport grant (round 5)."""
    return cmd.prog in ("gog", "gws") and (_gog_env_wrapped(cmd) or _gog_foreign_cred(cmd.argv))


def _gog_foreign_cred(argv: List[str]) -> bool:
    """A flag ANYWHERE in the line (before or after the group, after another flag's value,
    or as `--flag=value`) that points gog at another client, home or token (fix round 2).
    These flags have no short forms in gog v0.25."""
    return any(a.split("=", 1)[0] in _GOG_FOREIGN_CRED_FLAGS for a in argv[1:] if a.startswith("--"))


def _gog_any_write(cmd: shell.Simple) -> bool:
    """An Omni turn's gog / gws command that is not a read and changes Google data: its group
    is read past known global flags only (an unknown one before it: not this), nothing before
    it points gog at another credential, and no word of it is in _GOG_NO_GENERIC (Gmail, Chat,
    the calendar, gog's own credentials / settings, servers, send / forward / draft words)."""
    if cmd.prog not in ("gog", "gws") or cmd.dynamic or _gog_read_only(cmd.argv) or _gog_foreign_cred(cmd.argv) or _gog_env_wrapped(cmd):
        return False
    if any(_GOG_NO_GENERIC.match(a) for a in cmd.argv[1:]):
        return False
    skip = False
    for a in cmd.argv[1:]:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            if ("=" in a and a.startswith("--")) or a in _GOG_BOOL_FLAGS:
                continue
            if a in _GOG_VALUE_FLAGS:
                skip = True
                continue
            return False
        return True
    return False


def _gog_cmd_words(argv: List[str]) -> List[str]:
    """gog's command words, lower-cased: every non-flag token, minus the values of the known
    global value flags, with the group alias normalised. Subcommand flags' values stay in
    (their arity is unknown), which can only make a notify check stricter."""
    out: List[str] = []
    skip = False
    for a in argv[1:]:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            skip = a in _GOG_VALUE_FLAGS
            continue
        out.append(a.lower())
    if out:
        out[0] = _GOG_ALIASES.get(out[0], out[0])
    return out


_GRANT_VERBS = frozenset({"create", "add", "update", "insert", "patch", "invite"})
_MENTION = re.compile(r"(^|[\s(\[,;:])[@+][\w.-]|[\w.+-]+@[\w-]+\.\w")


def _gog_shares(argv: List[str]) -> bool:
    w = _gog_cmd_words(argv)
    if w[:1] == ["keep"]:
        return False                                   # send.keep-share
    if "share" in w[:2]:
        return True
    return any(x in ("permissions", "permission", "perms") and y in _GRANT_VERBS for x, y in zip(w[1:3], w[2:4]))


def _gog_group_adds(argv: List[str]) -> bool:
    w = _gog_cmd_words(argv)
    return w[:1] == ["groups"] and any(x in ("add", "insert", "invite", "add-member", "add-members") for x in w[1:3])


def _gog_classroom_notifies(argv: List[str]) -> bool:
    # Every classroom / class command that is not a read (round 4): nearly all of them notify
    # students, teachers or guardians. Help and version stay readable.
    w = _gog_cmd_words(argv)
    if w[:1] != ["classroom"] or any(a in ("--help", "-h", "--version") for a in argv[1:]):
        return False
    return not _gog_classroom_read(w)


def _gog_comment_mentions(argv: List[str]) -> bool:
    w = _gog_cmd_words(argv)
    if len(w) < 3 or w[1] not in ("comments", "comment") or w[2] not in ("add", "create", "new", "reply", "respond", "insert"):
        return False
    if any(a.split("=", 1)[0].startswith(("--assign", "--mention")) for a in argv[1:] if a.startswith("--")):
        return True
    skip = False
    for a in argv[1:]:                                 # the text, not the global flags' values (`-a me@x`)
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            name, eq, val = a.partition("=")
            skip = not eq and a in _GOG_VALUE_FLAGS
            if eq and name not in _GOG_VALUE_FLAGS and _MENTION.search(val):
                return True
            continue
        if _MENTION.search(a):
            return True
    return False


def _gog_keep_shares(argv: List[str]) -> bool:
    w = _gog_cmd_words(argv)
    return w[:1] == ["keep"] and ("share" in w[1:3] or any(x in ("collaborators", "collaborator") and y in _GRANT_VERBS
                                                          for x, y in zip(w[1:3], w[2:4])))


def _gog_head(argv: List[str], limit: int = 5) -> Tuple[List[str], bool]:
    """gog's leading COMMAND words (lower-cased, group alias normalised) and whether they were read
    cleanly: global flags (and their values) are skipped; the first flag that is not a known global
    one ends the head — a value after it may be free text. `cut` is True when that happened before
    two words were found, i.e. the subcommand is unknown (the caller fails closed)."""
    words: List[str] = []
    skip = False
    cut = False
    for a in argv[1:]:
        if skip:
            skip = False
            continue
        if a.startswith("-") and a != "-":
            if a in _GOG_VALUE_FLAGS:
                skip = True
                continue
            if a in _GOG_BOOL_FLAGS or (a.startswith("--") and "=" in a and a.split("=", 1)[0] in _GOG_VALUE_FLAGS):
                continue
            cut = len(words) < 2
            break
        words.append(a.lower())
        if len(words) >= limit:
            break
    if words:
        words[0] = _GOG_ALIASES.get(words[0], words[0])
    return words, cut


def _gog_flag_names(argv: List[str]) -> List[Tuple[str, Optional[str]]]:
    """(flag name, value-if-known) for every `--flag` token (`--x=v` → v, `--x v` → the next token)."""
    out = []
    for i, a in enumerate(argv[1:], start=1):
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            out.append((name.lower(), val if eq else (argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else None)))
    return out


_GMAIL_READ_VERBS = frozenset({"search", "find", "query", "ls", "list", "get", "info", "show", "raw", "attachment", "url", "history",
                               "labels", "label", "thread", "threads", "read", "messages", "message", "msg", "msgs"})
_GMAIL_SEND_VERBS = frozenset({"send", "reply", "forward", "fwd", "autoreply", "+send", "+reply", "+forward"})


def _gog_gmail_send(argv: List[str]) -> bool:
    """Gmail send / reply / forward / autoreply / draft send-create-update / messages send-insert, and a
    top-level `gog send`. Judged on command words only; an unreadable head fails closed."""
    w, cut = _gog_head(argv)
    if cut or not w:
        return True
    if w[0] in ("send", "reply", "forward", "fwd"):
        return True
    if w[0] != "gmail":
        return False
    rest = w[1:]
    if rest[:1] and rest[0] in _GMAIL_READ_VERBS and rest[0] not in ("messages", "message", "msg", "msgs"):
        return False
    if any(x in _GMAIL_SEND_VERBS for x in rest):
        return True
    for x, y in zip(rest, rest[1:]):
        if x in ("drafts", "draft") and y in ("send", "create", "update", "new"):
            return True
        if x in ("messages", "message", "msg", "msgs") and y in ("send", "insert", "import"):
            return True
    return False


_GMAIL_SETTING_WORDS = frozenset({"settings", "forwarding", "forward-addresses", "autoforward", "auto-forward", "vacation",
                                 "sendas", "send-as", "delegates", "delegate"})
_SETTING_READS = frozenset({"list", "ls", "get", "show", "info"})


#: Fix round 2 (N4): gws's Gmail settings resources and methods (Gmail API names, lower-cased).
_GWS_SETTING_RESOURCES = frozenset({"forwardingaddresses", "autoforwarding", "vacation", "sendas", "delegates", "imap", "pop",
                                    "updateautoforwarding", "updatevacation", "updateimap", "updatepop", "smimeinfo"})


def _gws_gmail_settings(rest: List[str], argv: List[str]) -> bool:
    """`gws gmail users settings <resource> <method>`: any settings change other than a read (get / list)
    is refused; a filter change is refused when its body or flags mention forwarding."""
    if "settings" not in rest:
        return False
    after = rest[rest.index("settings") + 1:]
    reading = any(x.startswith(("get", "list")) for x in after)
    if any(x in ("filters", "filter") for x in after):
        if reading:
            return False
        return any("forward" in a.lower() for a in argv[1:])
    if not after:
        return False                                   # `gws gmail users settings` alone: help
    return not reading


def _gog_gmail_settings(argv: List[str]) -> bool:
    """Fix round 1 (I2): a Gmail settings change that reaches people (forwarding, auto-forward, vacation,
    send-as, delegates), or a filter create / update with a forward action. Reads (list / get) run."""
    w, cut = _gog_head(argv, limit=6)
    if cut:
        return True
    if w[:1] != ["gmail"]:
        return False
    rest = w[1:]
    if argv and shell.basename(argv[0]) == "gws":
        return _gws_gmail_settings(rest, argv)
    if any(x in _GMAIL_SETTING_WORDS - {"settings"} for x in rest) or (rest[:1] == ["settings"] and len(rest) >= 2
                                                                          and rest[1] not in ("filters", "filter", "labels")):
        return not any(x in _SETTING_READS for x in rest)
    if any(x in ("filters", "filter") for x in rest) and not any(x in _SETTING_READS for x in rest):
        return any(n.startswith("--forward") for n, _v in _gog_flag_names(argv))
    return False


_CHAT_READ_VERBS = frozenset({"list", "ls", "get", "info", "show", "search", "find"})


def _gog_chat_write(argv: List[str]) -> bool:
    """Any Google Chat command that is not a read (list / get / search …) or bare help."""
    w, cut = _gog_head(argv)
    if cut:
        return True
    if not w or w[0] != "chat" or len(w) < 2:
        return False
    return not (len(w) >= 3 and w[2] in _CHAT_READ_VERBS)


#: gog v0.25 calendar verbs (and aliases) that write an event.
_CAL_EVENT_WRITES = frozenset({"create", "add", "new", "update", "edit", "set", "move", "transfer", "delete", "rm", "del", "remove",
                               "insert", "patch", "import", "quick-add", "quickadd"})
_CAL_RSVP = frozenset({"respond", "rsvp", "reply"})
#: Blocks of the owner's own time (work unblock 13): they decline other people's invitations unless
#: `--auto-decline none` — gog's default is `all`.
_CAL_DECLINERS = frozenset({"focus-time", "focus", "out-of-office", "ooo"})
#: Other own-calendar writes: working location, subscribing to / creating a calendar.
_CAL_OWN_OTHER = frozenset({"working-location", "wl", "subscribe", "sub", "add-calendar", "create-calendar", "new-calendar"})
_CAL_NOTIFY_FLAGS = frozenset({"--attendees", "--attendee", "--add-attendees", "--add-attendee", "--notify", "--send-updates", "--send-update"})


def _gog_calendar_rsvp(argv: List[str]) -> bool:
    w, cut = _gog_head(argv)
    return cut or (w[:1] == ["calendar"] and len(w) >= 2 and w[1] in _CAL_RSVP)


def _gog_calendar_invite(argv: List[str]) -> bool:
    """An event write that names attendees or asks Google to notify (`--send-updates` other than none)."""
    w, cut = _gog_head(argv)
    if not cut and not (w[:1] == ["calendar"] and len(w) >= 2 and w[1] in _CAL_EVENT_WRITES | _CAL_DECLINERS | _CAL_OWN_OTHER):
        return False
    for name, val in _gog_flag_names(argv):
        if name in _CAL_NOTIFY_FLAGS and not (name.startswith("--send-update") and (val or "").strip().lower() == "none"):
            return True
    return False


def _gog_calendar_declines(argv: List[str]) -> bool:
    """`propose-time` (it exists to tell the organizer), focus time / out of office without an explicit
    `--auto-decline none`, or an event write whose focus / out-of-office type or `--*-auto-decline`
    flag declines other people's invitations."""
    w, cut = _gog_head(argv)
    if cut:
        return True
    if w[:1] != ["calendar"] or len(w) < 2:
        return False
    flags = dict(_gog_flag_names(argv))
    none = lambda name: (flags.get(name) or "").strip().lower() == "none"  # noqa: E731
    if w[1] == "propose-time":
        return True
    if w[1] in _CAL_DECLINERS:
        return not none("--auto-decline")
    if w[1] in _CAL_EVENT_WRITES:
        kind = (flags.get("--event-type") or "").strip().lower().replace("_", "-")
        for flag in ("--focus-auto-decline", "--ooo-auto-decline"):
            if flag in flags and not none(flag):
                return True
        if kind in ("focus-time", "focustime") and not none("--focus-auto-decline"):
            return True
        if kind in ("out-of-office", "outofoffice", "ooo") and not none("--ooo-auto-decline"):
            return True
    return False


def _gog_calendar_own_write(argv: List[str]) -> bool:
    """sys.calendar-write hands out gog's credential: only for a calendar event write (create / add /
    update / edit / move / delete …) — read from the command words. Attendee / notify forms were
    refused by send.calendar-invite before this row is reached."""
    w, cut = _gog_head(argv)
    return not cut and w[:1] == ["calendar"] and len(w) >= 2 and w[1] in _CAL_EVENT_WRITES | _CAL_DECLINERS | _CAL_OWN_OTHER


_GMAIL_TIDY_TOP = frozenset({"archive", "trash", "untrash", "mark-read", "read-messages", "unread", "mark-unread"})
_GMAIL_TIDY_GROUPS = frozenset({"messages", "message", "msg", "msgs", "thread", "threads", "read", "batch"})
_GMAIL_MODIFY = frozenset({"modify", "update", "edit", "set"})
_GMAIL_LABEL_VERBS = frozenset({"list", "ls", "get", "info", "show", "create", "add", "new", "rename", "mv", "style", "color", "colour",
                                "delete", "rm", "del"}) | _GMAIL_MODIFY


def _gog_gmail_housekeeping(argv: List[str]) -> bool:
    """Gmail housekeeping on command words: archive / trash / untrash / mark read or unread, a label
    change on messages / threads (modify and its aliases; `batch modify`, never `batch delete`), and
    labels list / create / modify. No other Gmail word anywhere in the head (settings, filters,
    forwarding, sendas, delegates, vacation, drafts, send …), and no foreign credential."""
    if _gog_foreign_cred(argv):
        return False
    w, cut = _gog_head(argv)
    if cut or w[:1] != ["gmail"] or len(w) < 2 or any(_GOG_NO_GENERIC.match(x) for x in w[1:]):
        return False
    verb = w[1]
    if verb in _GMAIL_TIDY_TOP:
        return True
    if len(w) < 3:
        return False
    if verb in _GMAIL_TIDY_GROUPS:
        return w[2] in _GMAIL_MODIFY
    if verb in ("labels", "label"):
        return w[2] in _GMAIL_LABEL_VERBS
    return False


def _gog_google_write(argv: List[str]) -> bool:
    """sys.google-write hands out gog's credential: only for a group that is not Gmail, Chat, the
    calendar or gog's own settings — read from the command words, never from argument text."""
    w, cut = _gog_head(argv)
    return bool(w) and not cut and not _GOG_NO_GENERIC.match(w[0])


#: SENDS rows judged on gog's command words (their table regex only pre-filters), so that
#: `gog docs insert D 'share the plan'` is not a share. Used by _matches on every path.
_GOG_NOTIFY_CHECKS = {"send.google-share": _gog_shares, "send.google-groups": _gog_group_adds,
                      "send.google-classroom": _gog_classroom_notifies, "send.google-comment-mention": _gog_comment_mentions,
                      "send.keep-share": _gog_keep_shares,
                      # work unblock 3: Gmail, Chat and calendar sends on command words, not argument text
                      "send.gog-gmail": _gog_gmail_send, "send.gog-chat": _gog_chat_write,
                      "send.calendar-rsvp": _gog_calendar_rsvp, "send.calendar-invite": _gog_calendar_invite,
                      "send.calendar-decline": _gog_calendar_declines, "send.gmail-settings": _gog_gmail_settings,
                      "sys.google-write": _gog_google_write, "sys.calendar-write": _gog_calendar_own_write,
                      "sys.gmail-housekeeping": _gog_gmail_housekeeping,
                      # a Gmail READER is a read on command words (`gmail thread modify` is housekeeping)
                      "read.gog-gmail": _gog_read_only}


def outward_secret(p: str) -> bool:
    """A credential place no turn may read (S2): paths.is_secret. This repo's `state/` keeps only
    its credential files secret (paths.state_secret — the same predicate on Omni turns since work
    unblock 5)."""
    return paths.is_secret(p)


#: Distinctive secret basenames a wildcard must not be able to sweep up (R2-1). Ordinary
#: names (`*.txt`) are not here; `*.db`/`*.sqlite` are, since a turn rarely needs to read a
#: database wholesale and Prism's sessions live in one — accepted over-refusal, documented.
_GLOB_SECRET_NAMES = (
    ".env", ".env.local", ".env.production", ".env.bak", "prism-server.db", "prism-server.db-wal",
    "prism-server.db-shm", "id_rsa", "id_ed25519", "id_ecdsa", "auth.json", "credentials.json",
    "client_secret.json", "token.json", "google_token.json", "google_client_secret.json", "oauth_token.json",
    "client_secret_1.apps.googleusercontent.com.json", "token.pickle", "proton-mail-state.json", "x.pem", "x.key", "x.p8", "x.keychain-db",
    "something.db", "something.sqlite", "something.sqlite3",
)
_GLOB_META = re.compile(r"[*?\[]")


def _glob_secret_word(word: str, cwd: Optional[str]) -> bool:
    """A shell word containing a wildcard whose expansion could reach a credential: its
    directory is a secret place, or its basename pattern matches a distinctive secret
    name. Non-glob words return False (the literal path check handles those)."""
    import fnmatch
    if not word or "\0" in word or "://" in word or not _GLOB_META.search(word):
        return False
    expanded = os.path.expanduser(word)
    base = os.path.basename(expanded)
    directory = os.path.dirname(expanded)
    # The fixed directory part (up to the first wildcard) — if it is, or is inside, a
    # secret dir / the Prism server dir, a wildcard there sweeps secrets.
    fixed = directory.split("*")[0].split("?")[0].split("[")[0]
    if fixed:
        rp = paths.real(fixed, cwd)
        server = paths.real(paths.HOME() + "/dev/prism/apps/server")
        if paths.holds_secret(rp) or paths.is_secret(rp) or rp == server or rp.startswith(server + "/"):
            return True
    if _GLOB_META.search(base):
        return any(fnmatch.fnmatch(name, base) for name in _GLOB_SECRET_NAMES)
    return False


def _secret_block() -> Decision:
    return _block("secret.path", "Read a credential or a private store",
                  "That path holds credentials, sessions or private messages; no Hermes turn reads it. The agent's own "
                  "scripts read what they need themselves — run the script, not the file.")


def _code_outward(code: str, cwd: Optional[str]) -> Optional[Decision]:
    """Inline / submitted code: self-approval, credential places, senders."""
    if _decides_for_owner(code):
        return _send_block("self.approve", "Decide an approval or run a Prism live action",
                           "Approving a draft (or acting through Prism's live actions) takes the owner's own device.")
    if _prism_control(code):
        return _block("self.prism-auth", "Drive Prism's sign-in, device or auth flow",
                      "Signing a device in, requesting a magic link, or reaching the Prism server on loopback is "
                      "never done from a turn — it could mint a credential that approves sends. The owner does this "
                      "on his device. (The send scripts verify their one-time grant themselves; a turn does not.)")
    if _google_rest_send(code):
        return _google_rest_block()
    if re.search(r"\b(google_api|gws_bridge)\b", _normalize_url_text(code), re.IGNORECASE):
        # Importing, exec'ing, copying or runpy-ing the sender from code: its functions send
        # with the Google token, and `getattr(m, 'se'+'nd')` defeats any text check (R5 I-c).
        return _send_block("send.google-api-code", "google_api.py or gws_bridge.py imported, copied or run from code",
                           "google_api.py and gws_bridge.py send as the signed-in Google account with no approval. Run their read "
                           "commands from the shell; draft sends with omni_propose (kind email or calendar-invite).")
    collapsed = re.sub(r"['\"]\s*\+\s*['\"]", "", code or "")
    if _code_secret_text(code) or _code_secret_text(collapsed) or any(outward_secret(p) for p in _code_paths(code, cwd)):
        return _secret_block()
    return None


def owner_target(target: Any) -> bool:
    """send_message's `target` is the owner's own chat: bare `telegram` (the home channel,
    TELEGRAM_HOME_CHANNEL = his chat) or `telegram:<his chat id>[:<topic>]`."""
    t = str(target or "").strip().lower()
    if t == "telegram":
        return True
    m = re.match(r"^telegram:(-?\d+)(?::\d+)?$", t)
    return bool(m and m.group(1) == OWNER_TELEGRAM_CHAT)


_FALLBACK = ("If the omni_propose tool is not offered on this surface, run `python3 scripts/omni_propose.py <kind> …` "
             "from the agent repo instead — it only proposes; nothing is sent until the owner approves that exact draft.")


def _send_block(rule: str, title: str, why: str) -> Decision:
    """A refused send always names the way: omni_propose, or its CLI where the tool is absent."""
    text = why if "omni_propose" in why else why + " " + T.PROPOSE
    return _block(rule, title, text + " " + _FALLBACK)


def decide_outward(tool: str, args: Optional[Dict[str, Any]], cwd: Optional[str] = None) -> Optional[Decision]:
    """The outward edge for a turn that is NOT an Omni turn: a BLOCK decision, or None
    (the call runs exactly as it would without this plugin). Never raises. A fault in
    here blocks only a call that looks outward — it must not brick Telegram."""
    try:
        return _decide_outward(tool or "", args if isinstance(args, dict) else {}, cwd, 0)
    except Exception:
        try:
            text = (tool or "") + " " + json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            text = tool or ""
        if _OUTWARD_TOOL.search(tool or "") or _LOOKS_OUTWARD.search(text):
            return _block("internal", "The outward check failed",
                          "omni-bridge could not judge this call, and it looks like it could reach someone; it was not run.")
        return None


_PKG_RUNNERS = frozenset({"uv", "uvx", "pipx", "poetry", "pdm", "rye", "hatch"})


def _hermes_words(cmd: shell.Simple) -> Optional[List[str]]:
    """If `cmd` runs the Hermes CLI (any way: the `hermes`/`hermes-acp` program,
    `python -m hermes_cli[.main]`, a `hermes_cli/main.py` path, or `uv run|uvx|pipx run
    hermes`), return the words AFTER the entrypoint (so the first non-flag is the verb).
    None when it is not Hermes (R2-3)."""
    if not cmd.argv:
        return None
    prog, script = cmd.prog, _script_of(cmd)
    if prog in _HERMES_PROGS:
        return cmd.argv[1:]
    if script and script.startswith("-m:"):
        mod = script[3:]
        if mod == "hermes_cli" or mod.startswith("hermes_cli."):
            i = cmd.argv.index("-m")
            return cmd.argv[i + 2:]
    if script and not script.startswith("-m:"):
        norm = script.replace("\\", "/")
        if re.search(r"(^|/)hermes_cli/(main|__main__|cli)\.py$", norm) or re.search(r"(^|/)hermes(-cli)?\.py$", norm):
            return cmd.argv[cmd.argv.index(script) + 1:]
    if prog in _PKG_RUNNERS:
        rest = cmd.argv[1:]
        for j, a in enumerate(rest):
            if a in ("run", "exec", "x"):
                continue
            if a.startswith("-"):
                continue
            if a == "hermes" or a.endswith("/hermes"):
                return rest[j + 1:]
            break
    return None


def _shell_outward(command: str, cwd: Optional[str]) -> Optional[Decision]:
    m = re.match(r"^\s*OMNI_NEEDS=([A-Za-z,_-]+)\s+", command)
    if m:
        command = command[m.end():]
    parsed = shell.parse(command)
    if _decides_for_owner(command):
        return _send_block("self.approve", "Decide an approval or run a Prism live action",
                           "Approving a draft (or acting through Prism's live actions) takes the owner's own device.")
    if _prism_control(command):
        return _block("self.prism-auth", "Drive Prism's sign-in, device or auth flow",
                      "Signing a device in, requesting a magic link, or reaching the Prism server on loopback is "
                      "never done from a turn — it could mint a credential that approves sends. The owner does this "
                      "on his device. (The send scripts verify their one-time grant themselves; a turn does not.)")
    if _google_rest_send(command):
        return _google_rest_block()
    for c in parsed.commands:
        if not c.argv:
            continue
        if _google_api_copy(c, command):
            return _send_block("send.google-api-copy", "Copy or link google_api.py or gws_bridge.py",
                               "A copy of google_api.py or gws_bridge.py under another name would send as the "
                               "signed-in Google account with no approval. Run the skill's script itself, for reads only.")
        bridge = _gws_bridge_args(c)
        if bridge is not None and not _gog_read_only(["gws"] + bridge):
            return _send_block("send.gws-bridge", "A gws_bridge.py command that is not a read",
                               "gws_bridge.py runs gws with the signed-in Google account's token, so off the Omni app "
                               "it runs only gws's read commands, exactly like gws itself. `gmail +send`, `users "
                               "messages send`, a calendar insert (attendees get an invitation) or a Drive share "
                               "reaches people with no approval.")
        argtext = " ".join(c.argv[1:])
        if (_INTERPRETERS.match(c.prog) or "/" in c.argv[0] or c.prog.endswith(".py")) and _GOOGLE_API_ARGS.search(argtext):
            return _send_block("send.google-api", "Gmail send/reply, a calendar write or a Drive share through a "
                               "google_api.py-shaped script", "Whatever the script is called, a `gmail send`, "
                               "`calendar create|update` or `drive share` sends with no approval.")
        if (_INTERPRETERS.match(c.prog) or "/" in c.argv[0] or c.prog.endswith(".py")) and _GWS_SEND_ARGS.search(argtext):
            return _send_block("send.gws-bridge", "A Gmail send, calendar insert or Drive share through a "
                               "gws_bridge.py-shaped script", "Whatever the script is called, `gmail +send`, `users "
                               "messages send`, `calendar +insert`, `events insert` or `permissions create` "
                               "sends with no approval.")
    hermes_here = any(_hermes_words(c) is not None for c in parsed.commands if c.argv)
    if hermes_here and re.search(r"\bHERMES_HOME\s*=", command):
        return _block("self.hermes-home", "Start Hermes with another home",
                      "A Hermes with another HERMES_HOME loads none of this guard. Run it from your own terminal.")
    for cmd in (c for c in parsed.commands if c.argv):
        prog0, script0, args0 = _facts(cmd)
        words = _hermes_words(cmd)
        if words is not None:
            verb = next((w for w in words if not w.startswith("-")), "")
            if verb in ("meet", "omni-meet"):
                sub = next((w for w in words[words.index(verb) + 1:] if not w.startswith("-")), "")
                if sub not in ("status", "transcript", "setup", "help", ""):
                    return _send_block("send.hermes-meet", "Join, speak in or sign in to a meeting from the shell",
                                       "Meetings go through omni_meet_attend / omni_meet_allow_speech, approved by "
                                       "The owner in the Omni app; the shell's meet commands bypass that.")
            if prog0 == "hermes-acp" or verb in ("send", "chat", "acp") or "HERMES_HOME" in cmd.assigns or \
                    any(w in ("-z", "--oneshot", "-q", "--query") for w in words):
                if verb == "send":
                    return _send_block("send.hermes-cli", "Hermes' own send command",
                                       "hermes send delivers with the gateway's tokens to anyone.")
                return _block("self.hermes-nested", "Start another Hermes agent from a turn",
                              "A nested Hermes agent would act outside this turn. Ask in this conversation instead.")
        if prog0 in ("gog", "gws") and not _gog_read_only(cmd.argv):
            for rule in T.SENDS:
                if _matches(rule, cmd, None):
                    return _send_block(rule.id, rule.title, rule.why)
            return _send_block("gog.read-only", "A gog command that is not a read",
                               "Off the Omni app, gog runs only its read commands (calendar/gmail/drive/docs/sheets/"
                               "forms/slides/contacts/tasks … reads). Anything that sends or changes Google data is drafted for the owner.")
        if prog0 == "osascript" and not cmd.code:
            # -e inline is judged by the send.applescript text rule below. A script FILE is
            # refused outright off Omni: it may be written moments earlier (`printf … >
            # s.scpt && osascript s.scpt`) so its content cannot be vetted, and no repo
            # AppleScript is expected.
            i = 1
            inline = False
            while i < len(cmd.argv):
                a = cmd.argv[i]
                if a == "-e":
                    inline = True
                    break
                if a in ("-l", "-s"):
                    i += 2
                    continue
                if a == "--":
                    i += 1
                    continue
                if a.startswith("-"):
                    i += 1
                    continue
                return _send_block("send.applescript", "Run an AppleScript file",
                                   "An AppleScript file can send as the owner in any signed-in app, and one written "
                                   "moments earlier cannot be vetted; no AppleScript file is run off the Omni app.")
            del inline
        if prog0 == "open":
            # `open imessage:/sms:/facetime:/tel:/…` can place a call or message as the owner.
            # (mailto: only opens a draft, so it is left alone.)
            if any(re.match(r"^(imessage|sms|facetime(-audio)?|tel|whatsapp|tg|sgnl|slack)://?", a, re.IGNORECASE) for a in cmd.argv[1:]):
                return _send_block("send.url-scheme", "Place a call or message via a URL scheme (open imessage:/sms:/…)",
                                   "Opening a messaging or call URL can reach someone as the owner.")
        # A gog line that passed the read-only allowlist above is a read (a search for the
        # word "send" included); every other gog line was refused there.
        for rule in (() if prog0 in ("gog", "gws") else T.SENDS):
            if _matches(rule, cmd, None):
                return _send_block(rule.id, rule.title, rule.why)
        # secret.browser-session is an Omni-turn rule (its sandbox has no signed-in browser); other
        # surfaces keep whatever browser the owner set up for them.
        for rule in tuple(r for r in T.SECRETS if r.id != "secret.browser-session") + tuple(r for r in T.SELF if r.id in _SELF_OUTWARD):
            if _matches(rule, cmd, None):
                return _block(rule.id, rule.title, rule.why)
        prog, script, argtext = _facts(cmd)
        if (script and shell.basename(script) == "send_telegram.py") or prog == "send_telegram.py":
            if re.search(r"--chat(-id)?\b|--to\b", argtext) or "TELEGRAM_CHAT_ID" in cmd.assigns:
                return _send_block("send.telegram-other", "A Telegram message to someone other than the owner",
                                   "send_telegram.py only ever reaches the owner's own chat.")
        if cmd.code and _CODE_SENDS.search(cmd.code):
            return _send_block("send.code", "Inline code that runs a sender", "Sends are never run from code.")
        if cmd.code and cmd.lang not in (None, "text"):
            d = _code_outward(cmd.code, cwd)
            if d is not None:
                return d
        _named, written = _paths_of(cmd, cwd)
        if any(outward_secret(p) for p in _named + written) or (cmd.prog in ("source", ".") and len(cmd.argv) > 1 and
                                                                re.search(r"(^|/)\.env\b", cmd.argv[1])):
            return _secret_block()
        if any(_glob_secret_word(a, cwd) for a in cmd.argv[1:] if not a.startswith("-")):
            return _secret_block()
        targets = list(written)
        if cmd.prog in _WRITERS and cmd.prog != "git":
            targets += _named
        for p in targets:
            if _outward_guard(p):
                return _self_files_block(p, "Change the guard, Hermes' settings or a sender")
    return None


def _text_sends(text: str) -> Optional[Decision]:
    if _decides_for_owner(text):
        return _send_block("self.approve", "Decide an approval or run a Prism live action",
                           "Approving a draft (or acting through Prism's live actions) takes the owner's own device.")
    if _prism_control(text):
        return _block("self.prism-auth", "Drive Prism's sign-in, device or auth flow",
                      "Signing a device in, requesting a magic link, or reaching the Prism server on loopback is "
                      "never done from a turn — it could mint a credential that approves sends. The owner does this "
                      "on his device. (The send scripts verify their one-time grant themselves; a turn does not.)")
    if _google_rest_send(text):
        return _google_rest_block()
    for rule in T.SENDS + T.SECRETS:
        _a, _u, text_re = T.compiled(rule)
        if text_re and text_re.search(text):
            return _send_block(rule.id, rule.title, rule.why) if rule in T.SENDS else _block(rule.id, rule.title, rule.why)
    if _CODE_SENDS.search(text):
        return _send_block("send.code", "Code that runs a sender", "Sends are never run from code.")
    return None


def _decide_outward(name: str, args: Dict[str, Any], cwd: Optional[str], depth: int) -> Optional[Decision]:
    if name == "tool_call" and isinstance(args.get("name"), str) and depth < 3:
        inner = args.get("arguments")
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                inner = {}
        return _decide_outward(args["name"], inner if isinstance(inner, dict) else {}, cwd, depth + 1)
    if name in ("tool_search", "tool_describe", "omni_propose"):
        return None
    if name == "skill_manage":
        # Every Hermes turn: a repo skill is code; an edit would dirty the production checkout and drift from it.
        return _skill_repo_block() if paths.skill_write_protected(args) else None
    if name == "terminal":
        wd = args.get("workdir") if isinstance(args.get("workdir"), str) and args.get("workdir") else cwd
        if wd and not os.path.isdir(wd):
            wd = None
        return _shell_outward(str(args.get("command") or ""), wd)
    if name == "execute_code":
        code = str(args.get("code") or "")
        return _text_sends(code) or _code_outward(code, cwd)
    if name in ("read_file", "search_files"):
        for p in _read_targets(args):
            if outward_secret(paths.real(p, cwd)):
                return _secret_block()
        return None
    if name in ("write_file", "patch"):
        for p in _patch_targets(args):
            if _outward_guard(paths.real(p, cwd)):
                return _self_files_block(paths.real(p, cwd), "Change the guard, Hermes' settings or a sender")
        return _text_sends(str(args.get("content") or "") + "\n" + str(args.get("patch") or "") +
                           "\n" + str(args.get("new_string") or args.get("new_str") or ""))
    if name in _MSG_TOOLS or name.startswith("send_"):
        action = str(args.get("action") or "send").strip().lower()
        if name == "send_message" and action == "list":
            return None
        if name in ("send_message", "react_to_message") and owner_target(args.get("target")):
            return None
        # No target at all: Hermes' own tool refuses it ("Both 'target' and 'message' are
        # required") and cron deliveries do not use the tool, so nothing can leave (N5).
        if name in ("send_message", "react_to_message") and not str(args.get("target") or "").strip():
            return None
        return _send_block("tool.send-message", "A message (or reaction) to someone other than the owner",
                           "Hermes' own send_message may only reach the owner's own chat (target 'telegram' or "
                           "'telegram:%s'). Messages to other people are never sent directly." % OWNER_TELEGRAM_CHAT)
    if name in T.MEET_DOOR_TOOLS or name in ("meet_join", "meet_say"):
        return _send_block("meet.door", "Send Omni to a meeting, or let it speak there",
                           "Attending a meeting and speaking in it are each approved by the owner in the Omni app. "
                           "Propose an action (omni_propose kind action) with grants meet.attend / meet.speak.")
    # Hermes v0.23 turned these toolsets on for every platform. Off Omni there is no approval card, and
    # a click or a new connector can reach anyone: desktop control and connection changes are Omni-only.
    if name == "computer_use" or name.startswith(("computer_", "desktop_")):
        return _send_block("tool.computer-use", "Control the Mac's screen, keyboard and mouse",
                           "Desktop control can click Send in any app; it runs only on an Omni turn, with approval.")
    if name == "manage_catalog" or (name == "manage_connections" and
                                    str(args.get("action") or "status").strip().lower() not in ("display", "status")):
        return _send_block("tool.connections", "Change Hermes' connections or tool catalog",
                           "A new connection is a new path for data in and out; this conversation cannot change its own tools.")
    mcp = _mcp_parts(name)
    if mcp is not None:
        if mcp_outward(mcp[1]):
            r = T.MCP_RULES[0]
            return _send_block(r[0], r[2], r[3])
        if mcp_connector_notifies(mcp[1]) and not (mcp[0] and mcp[0] in mcp_servers()):
            return _send_block("mcp.untrusted-write", "A comment or reply through a connector",
                               "Comments notify the item's collaborators; they run only on an Omni turn, with approval.")
        return None
    if _OUTWARD_TOOL.search(name):
        return _send_block("tool.outward-name", "A tool that sends", "Sends go through an approval card.")
    return None
