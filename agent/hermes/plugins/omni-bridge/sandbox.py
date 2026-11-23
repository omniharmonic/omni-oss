"""The OS sandbox every Omni shell command runs in (macOS `sandbox-exec`).

This is the enforcement of the Omni tool policy. The command reader (shell.py)
and the rule table (guard_table.py) decide what a command is ALLOWED beyond the
default; this decides what it CAN do, in the kernel, whatever it turns out to
be — a one-liner, a script, a pipeline the reader did not understand, a child
of a child. A sandboxed process cannot leave its sandbox and neither can
anything it starts.

Tiers (a command gets exactly one):

  contained  the default. Reads everything except secrets; writes only in the
             workspace and scratch directories; no network at all (not even to
             this machine, unless a port is listed in OMNI_BRIDGE_LOOPBACK_PORTS);
             no Keychain; only a short list of system services; an environment
             with no secret in it.
  reader     a trusted read-only command from the table (`gog calendar …`,
             `scripts/omni_cli.py tasks`). As `contained`, plus exactly what
             its row grants: the network, the Keychain, the repo's `.env`.
  approved   the owner approved this exact command on his device. Network on,
             writes anywhere except the guard's own files and the secrets.
             Still no secret reads and no Keychain — unless the program is a
             transport that needs its own credential (git/ssh/gh), or a
             trusted repo script.

Verified on macOS 26 (Darwin 25): under `contained`, `cat .env`, `python
open()`, `cp`, a symlink, `base64 <`, `tar` all fail with "Operation not
permitted"; TCP and UDP to any non-loopback address fail; a Keychain item is
"not found"; `sandbox-exec` inside the sandbox cannot widen it; setuid
programs (`ps`, `sudo`, `crontab`, `at`) cannot be executed at all.

The ways OUT of a sandbox are closed in every tier: a command cannot hand
work to a process outside it. `launchctl submit` (a launchd job runs
unsandboxed), AppleEvents (`osascript -e 'tell application "Terminal" …'`),
launching an app (`open`), the clipboard, and — under `contained` — any local
socket (tmux, docker, an ssh agent) and any system service not on a short
list. Only a command the owner approved that IS one of those programs
(`launchctl kickstart …`) gets them back.

If `sandbox-exec` is missing or does not work on a machine, `available()` is
False and the policy asks the owner before EVERY shell command instead.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import Dict, Iterable, List, Optional, Sequence

from . import paths

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
#: How every wrapped command line begins (contain.py tells a wrapped command by it).
WRAP_PREFIX = '__omni_d="$(/usr/bin/mktemp -t omni-cwd)"; '

#: Environment an Omni command sees: these names from Hermes' own environment,
#: nothing else. No `*_KEY`, `*_TOKEN`, `*_SECRET` can be added (see env_names()).
ENV_ALLOW = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "TERM",
             "COLUMNS", "LINES", "SHELL", "PWD", "OLDPWD", "EDITOR", "PAGER",
             "OMNI_KB", "PROJECT_ROOT", "OMNI_REPO",
             # Who the owner is (public variant: identity comes from the environment, not the code).
             # Names and addresses only; never a credential.
             "OMNI_OWNER_NAME", "OMNI_OWNER_EMAIL", "OMNI_OWNER_EMAILS", "OMNI_AGENT_EMAIL",
             "OMNI_TIMEZONE", "PROTON_BRIDGE_USER")
_SECRET_NAME = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PRIVATE", "CREDENTIAL", "COOKIE", "SESSION", "AUTH")

_KEYCHAIN_SERVICES = ("com.apple.SecurityServer", "com.apple.securityd.xpc", "com.apple.secd",
                      "com.apple.security.agent", "com.apple.security.authhost", "com.apple.securityd",
                      "com.apple.ctkd.token-client", "com.apple.CryptoTokenKit.AuthenticationHintsProvider.agent.libxpc")
_DNS_SERVICES = ("com.apple.dnssd.service", "com.apple.mDNSResponder", "com.apple.mDNSResponderHelper")

#: System services a `contained` command may talk to. Everything else is refused: a
#: service is a process OUTSIDE the sandbox, and one that acts on request is a way out.
_MACH_BASE = (
    "com.apple.system.opendirectoryd.libinfo", "com.apple.system.opendirectoryd.membership",
    "com.apple.system.notification_center", "com.apple.system.logger", "com.apple.logd",
    "com.apple.diagnosticd", "com.apple.cfprefsd.daemon", "com.apple.cfprefsd.agent",
    "com.apple.bsd.dirhelper", "com.apple.CoreServices.coreservicesd", "com.apple.lsd.mapdb",
    "com.apple.FSEvents", "com.apple.fonts", "com.apple.FontObjectsServer",
    "com.apple.distributed_notifications@Uv3", "com.apple.SystemConfiguration.configd",
    "com.apple.coreservices.quarantine-resolver", "com.apple.DiskArbitration.diskarbitrationd",
    "com.apple.dyld.closured", "com.apple.analyticsd", "com.apple.metadata.mds",
    "com.apple.metadata.mds.legacy", "com.apple.trustd", "com.apple.trustd.agent", "com.apple.ocspd",
)
#: The clipboard (it may hold a password) and the services that start or drive other apps.
_ESCAPE_SERVICES = ("com.apple.pasteboard.1", "com.apple.coreservices.launchservicesd",
                    "com.apple.coreservices.appleevents", "com.apple.tccd", "com.apple.tccd.system")
#: Programs whose whole purpose is to make something OUTSIDE the sandbox act.
_ESCAPE_PROGRAMS = ("/bin/launchctl", "/usr/bin/osascript", "/usr/bin/open", "/usr/bin/shortcuts",
                    "/usr/bin/automator", "/usr/bin/pbcopy", "/usr/bin/pbpaste", "/usr/bin/at",
                    "/usr/bin/batch", "/usr/bin/crontab")

#: Runs the sandbox in a session of its own. In its own session (process group) the sandbox's
#: `(target others)` rules cover Hermes' shell that started it, and every other process Hermes runs:
#: inside the SAME group macOS lets a sandboxed process read its relatives' arguments and
#: ENVIRONMENT (KERN_PROCARGS2) whatever the profile says — and that shell's environment is Hermes'
#: (found 2026-10-22, work unblock 8). Hermes sweeps a timed-out command's descendants whatever
#: their session. If setsid is refused (already a group leader), a child does it.
NEW_SESSION = ("/usr/bin/perl -MPOSIX -e " + shlex.quote(
    "if (POSIX::setsid() < 0) { my $p = fork(); exit 126 unless defined $p; "
    "if ($p) { waitpid($p, 0); exit($? & 127 ? 128 + ($? & 127) : $? >> 8) } POSIX::setsid(); } "
    "exec { $ARGV[0] } @ARGV; exit 127;") + " --")

_available: Optional[bool] = None


def available() -> bool:
    """Does the sandbox work here? Tried once per process, with a real denial."""
    global _available
    if _available is None:
        try:
            profile = '(version 1)(allow default)(deny file-read* (literal "/private/etc/hosts"))'
            ok = subprocess.run([SANDBOX_EXEC, "-p", profile, "/bin/cat", "/usr/share/dict/README"],
                                capture_output=True, timeout=10).returncode == 0
            denied = subprocess.run([SANDBOX_EXEC, "-p", profile, "/bin/cat", "/etc/hosts"],
                                    capture_output=True, timeout=10).returncode != 0
            _available = ok and denied
        except Exception:
            _available = False
    return bool(_available)


def _reset_for_tests(value: Optional[bool] = None) -> None:
    global _available
    _available = value


def _q(s: str) -> str:
    """A string literal in the profile language."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rx(pattern: str) -> str:
    return '(regex #"%s")' % pattern.replace('"', '\\"')


def _subpaths(items: Iterable[str]) -> str:
    out = []
    for p in items:
        if not p:
            continue
        out.append("(subpath %s)" % _q(p))
        out.append("(literal %s)" % _q(p))
    return " ".join(out)


def loopback_ports() -> List[int]:
    """Ports on THIS machine a contained command may reach (OMNI_BRIDGE_LOOPBACK_PORTS,
    default none). A local port is only harmless when what listens there asks for a
    credential — which a contained command cannot read."""
    out: List[int] = []
    for item in os.environ.get("OMNI_BRIDGE_LOOPBACK_PORTS", "").split(","):
        item = item.strip()
        if item.isdigit() and 0 < int(item) < 65536 and int(item) not in (53, 5353) and int(item) not in out \
                and int(item) not in browser_cdp_ports():
            out.append(int(item))
    return out


#: The DevTools port of Omni's signed-in Chrome (launchd/com.example.omni-chrome.plist).
BROWSER_CDP_PORT = 9333


def browser_cdp_ports() -> List[int]:
    """Loopback ports a browser with sign-ins listens on for DevTools: the default above, any port in
    BROWSER_CDP_URL, and any in Hermes' `browser.cdp_url`. A shell that reached one could drive that browser."""
    out = [BROWSER_CDP_PORT]
    texts = [os.environ.get("BROWSER_CDP_URL", "")]
    try:
        with open(os.path.join(paths.hermes_home(), "config.yaml"), encoding="utf-8") as f:
            texts += re.findall(r"(?m)^\s+cdp_url:\s*(\S+)", f.read())
    except OSError:
        pass
    for t in texts:
        m = re.search(r"(?:localhost|127\.0\.0\.1|\[::1\]):(\d{1,5})", t or "")
        if m and 0 < int(m.group(1)) < 65536 and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return out


def _mach_extra() -> List[str]:
    return [n.strip() for n in os.environ.get("OMNI_BRIDGE_MACH_ALLOW", "").split(",")
            if n.strip() and re.match(r"^[A-Za-z0-9._@-]+$", n.strip())
            and n.strip() not in _ESCAPE_SERVICES and n.strip() not in _KEYCHAIN_SERVICES]


def _names(services: Iterable[str]) -> str:
    return " ".join("(global-name %s)" % _q(s) for s in services)


def env_names() -> List[str]:
    """The environment names passed through: the fixed list plus OMNI_BRIDGE_ENV_KEEP,
    minus anything that looks like a credential by name."""
    extra = [n.strip() for n in os.environ.get("OMNI_BRIDGE_ENV_KEEP", "").split(",") if n.strip()]
    names = []
    for n in list(ENV_ALLOW) + extra:
        up = n.upper()
        if any(mark in up for mark in _SECRET_NAME) or not n.replace("_", "").isalnum():
            continue
        if n not in names:
            names.append(n)
    return names


def profile(tier: str, *, network: bool = False, keychain: bool = False,
            read_secrets: Sequence[str] = (), write_anywhere: bool = False,
            extra_write: Sequence[str] = (), automation: bool = False, browser: bool = False) -> str:
    """The sandbox profile for one command. Later rules win over earlier ones.

    `automation` (an approved command that IS launchctl / osascript / open …)
    is the only thing that re-opens the ways out of the sandbox.
    """
    contained = tier != "approved" and tier != "reader"
    lines = ["(version 1)", "(allow default)"]

    # ── reads: secrets are unreadable, by name anywhere and by place ──
    names = " ".join(_rx(p) for p in paths.SECRET_NAME_PATTERNS) + " " + _rx(paths.state_secret_regex())
    lines.append("(deny file-read* %s %s)" % (names, _subpaths(paths.secret_dirs())))
    lines.append("(allow file-read* %s)" % " ".join(_rx(p) for p in paths.SECRET_NAME_EXCEPTIONS))
    # Fix round 1 (I1): the exceptions re-open public NAMES only, never a file inside a secret place.
    lines.append("(deny file-read* %s %s)" % (_rx(paths.state_secret_regex()), _subpaths(paths.secret_dirs())))
    # Listing a directory's NAME is not reading its content, and tools stat their way down a path.
    lines.append("(allow file-read-metadata)")
    if read_secrets:
        lines.append("(allow file-read* %s)" % _subpaths(read_secrets))

    # SecItem queries in a trusted reader still require kernel read permission
    # on the encrypted login database, in addition to the SecurityServer grant.
    # Only the two standard login files; no directory, other store, write or
    # `security` subprocess grant. The command/file policy still blocks raw reads.
    if keychain and tier in ("reader", "approved"):
        stores = [os.path.realpath(os.path.join(paths.HOME(), "Library", "Keychains", name))
                  for name in ("login.keychain-db", "login.keychain")]
        lines.append("(allow file-read-data %s)" % " ".join("(literal %s)" % _q(p) for p in stores))

    # ── writes ──
    if write_anywhere:
        lines.append("(deny file-write* %s %s)" % (_subpaths(paths.guard_paths()), _subpaths(paths.secret_dirs())))
        lines.append("(deny file-write* %s %s)" % (names, _rx(paths.state_guard_regex())))
    else:
        lines.append('(deny file-write* (subpath "/"))')
        ok = list(paths.workspace_roots()) + list(paths.scratch_roots()) + list(extra_write)
        lines.append("(allow file-write* %s)" % _subpaths(ok))
        # Even inside a writable place, a credential-named file stays out of reach — and so
        # do the guard's own files and the secret stores, should a workspace ever contain one.
        lines.append("(deny file-write* %s %s)" % (names, _rx(paths.state_guard_regex())))
        lines.append("(deny file-write* %s %s)" % (_subpaths(paths.guard_paths()), _subpaths(paths.secret_dirs())))
    # Fix round 3 (R1): the ancestors of the trusted binaries cannot be renamed, removed or replaced
    # (literal only: a new file inside them is still fine).
    anchors = paths.anchor_paths()
    if anchors:
        lines.append("(deny file-write* %s)" % " ".join("(literal %s)" % _q(a) for a in anchors))

    # ── system services ──
    if contained:
        allow = list(_MACH_BASE) + _mach_extra()
        if network:
            allow += list(_DNS_SERVICES)
        if keychain:
            allow += list(_KEYCHAIN_SERVICES)
        lines.append("(deny mach-lookup)")
        lines.append("(allow mach-lookup %s)" % _names(allow))
    elif not keychain:
        lines.append("(deny mach-lookup %s)" % _names(_KEYCHAIN_SERVICES))

    # ── network ──
    if not network:
        lines.append("(deny network*)")               # TCP, UDP and local sockets alike
        if contained:
            for port in loopback_ports():
                lines.append('(allow network-outbound (remote ip "localhost:%d"))' % port)
        else:
            lines.append('(allow network-outbound (remote ip "localhost:*"))')
            lines.append('(allow network-bind (local ip "localhost:*"))')
            lines.append('(allow network-inbound (local ip "localhost:*"))')
            # A resolver on loopback would forward a lookup — and a lookup can carry data out.
            lines.append('(deny network-outbound (remote ip "localhost:53") (remote ip "localhost:5353"))')
        if not contained:
            lines.append("(deny mach-lookup %s)" % _names(_DNS_SERVICES))
    elif contained:
        # An allow-listed fetch: the internet, but no local socket (docker, tmux, an ssh agent)
        # other than the name resolver's.
        lines.append('(deny network-outbound (subpath "/"))')
        lines.append('(allow network-outbound (literal "/private/var/run/mDNSResponder"))')
    # Omni's signed-in Chrome is driven over a loopback DevTools port; only Hermes' own browser tools may reach it
    # (they are judged per call), never a shell command in any tier.
    if network or not contained:
        ports = " ".join('(remote ip "localhost:%d")' % p for p in browser_cdp_ports())
        if ports:
            lines.append("(deny network-outbound %s)" % ports)

    # ── the ways out: launchd jobs, AppleEvents, launching apps, the clipboard ──
    if not automation:
        lines.append("(deny job-creation)")
        lines.append("(deny appleevent-send)")
        lines.append("(deny lsopen)")
        lines.append("(deny mach-lookup %s)" % _names(_ESCAPE_SERVICES))
        lines.append("(deny process-exec %s)" % " ".join("(literal %s)" % _q(x) for x in _ESCAPE_PROGRAMS))

    # ── a signed-out Chrome (work unblock 2, audit 3.3) ──
    if browser:
        # Chrome cannot start without asking LaunchServices about itself. App launching stays
        # closed (`lsopen`, AppleEvents, the clipboard, tccd are still denied above).
        lines.append("(allow mach-lookup %s)" % _names(("com.apple.coreservices.launchservicesd",)))
        # Its crash database, inside the owner's (secret) Chrome directory: written freely, but only
        # the database itself and dumps in flight are readable — not his Chrome's past reports.
        crash = paths.chrome_crashpad()
        lines.append("(allow file-write* (subpath %s))" % _q(crash))
        lines.append("(allow file-read* (literal %s) (literal %s) (subpath %s))"
                     % (_q(crash), _q(crash + "/settings.dat"), _q(crash + "/new")))
        # The agent's own profiles keep their cookie / password stores (credential-named files).
        roots = paths.browser_profile_roots()
        lines.append("(allow file-read* file-write* %s)" % _subpaths(roots))

    # ── the tool that prints from the Keychain ──
    lines.append('(deny process-exec (literal "/usr/bin/security"))')
    # Other processes' arguments and environment (`ps e`, sysctl kern.procargs2) hold tokens.
    lines.append("(deny process-info* (target others))")
    # Matched by PREFIX: the kernel names the lookup `kern.procargs2.<pid>`, so the exact name never
    # matched and any command could read a same-user process's argv AND environment (pm2's services,
    # launchd jobs) by MIB number (found 2026-10-22, work unblock 8). Its own process stays readable.
    # Do not grant process-info-pidinfo for others (what lsof needs): it re-opens the same read.
    lines.append('(deny sysctl-read (sysctl-name-prefix "kern.procargs"))')
    return "\n".join(lines)


def wrap(command: str, prof: str, *, tier: str, cwd: Optional[str] = None,
         env_extra: Optional[Dict[str, str]] = None, trusted_env: Sequence[str] = (), path: Optional[str] = None) -> str:
    """The command line Hermes' terminal runs instead of `command`: the same
    command, inside the sandbox, with a clean environment.

    The working directory survives: the inner shell writes where it ended up,
    and the outer shell (Hermes') follows, so `cd` behaves as it always did.
    """
    if any(name != "OMNI_SERVICE_TOKEN" for name in trusted_env):
        raise ValueError("Unknown trusted application environment capability")
    credentialed = bool(path) or bool(trusted_env) or tier == "approved"  # approved re-wraps (contain.py) carry no path
    # A credentialed run never inherits a Python start-up hook: no PYTHON* name passes through (even if listed in
    # OMNI_BRIDGE_ENV_KEEP) and the user site (usercustomize, .pth) is off. The interpreters' own site-packages are guard paths.
    pass_env = " ".join('%s="${%s-}"' % (n, n) for n in env_names() if not (credentialed and n.upper().startswith("PYTHON")))
    # A credentialed command gets paths.trusted_path() (fix round 2): the PATH policy._exec_trusted
    # resolved its programs on, with no directory an Omni turn could plant a program in.
    fixed = {"PATH": path or os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"), "OMNI_SANDBOX": tier}
    fixed.update(env_extra or {})
    if credentialed:
        fixed.pop("PYTHONPATH", None); fixed.pop("PYTHONSTARTUP", None); fixed.pop("PYTHONHOME", None)
        fixed["PYTHONNOUSERSITE"] = "1"
    fixed_env = " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in fixed.items())
    inner = command + '\n__omni_rc=$?; pwd -P > "$0" 2>/dev/null; exit $__omni_rc'
    go = ("cd %s 2>/dev/null; " % shlex.quote(cwd)) if cwd else ""
    run = "%s %s -p %s /bin/bash -c %s" % (NEW_SESSION, SANDBOX_EXEC, shlex.quote(prof), shlex.quote(inner))
    if trusted_env:
        # The credential travels on a private FD, never env utility argv or a
        # literal in the rewritten command/card. Close the FD before the reader.
        bootstrap = 'IFS= read -r OMNI_SERVICE_TOKEN <&3; export OMNI_SERVICE_TOKEN; exec 3<&-; exec ' + run + ' "$1"'
        run = '/bin/bash -c %s omni-env "$__omni_d" 3<<<"${OMNI_SERVICE_TOKEN-}"' % shlex.quote(bootstrap)
    else:
        run += ' "$__omni_d"'
    return WRAP_PREFIX + (
        "%s"
        "/usr/bin/env -i %s %s %s; __omni_rc=$?; "
        '__omni_n="$(/bin/cat "$__omni_d" 2>/dev/null)"; /bin/rm "$__omni_d" 2>/dev/null; '
        '[ -n "$__omni_n" ] && cd "$__omni_n" 2>/dev/null; (exit $__omni_rc)'
    ) % (go, pass_env, fixed_env, run)
