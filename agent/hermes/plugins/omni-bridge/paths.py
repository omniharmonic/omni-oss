"""Where things are, for the Omni tool policy: the secrets nobody on an Omni turn
may read, the places an Omni turn may write, and the files that ARE the guard.

Everything is computed when asked (environment and home can differ per process)
and returned as real paths: symlinks and `..` are resolved before any rule looks.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

HOME = lambda: os.path.realpath(os.path.expanduser("~"))  # noqa: E731


def real(path: str, cwd: Optional[str] = None) -> str:
    """Absolute real path of something a command named (it need not exist)."""
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    try:
        return os.path.realpath(p)
    except (OSError, ValueError):
        return os.path.normpath(p)


def repo_root() -> str:
    """The agent repo this plugin lives in (…/hermes/plugins/omni-bridge → 3 up)."""
    return str(Path(__file__).resolve().parents[3])


def hermes_home() -> str:
    return real(os.environ.get("HERMES_HOME", "") or "~/.hermes")


def _split(value: str) -> List[str]:
    return [real(p) for p in value.split(":") if p.strip()]


# ── secrets ──────────────────────────────────────────────────────────────────

#: File NAMES that hold a credential wherever they sit. (regex on the real path)
SECRET_NAME_PATTERNS: Tuple[str, ...] = (
    r"/[.]env($|[.])",                       # .env, .env.local, .env.bak-… (examples re-allowed below)
    r"/id_(rsa|dsa|ecdsa|ed25519)[^/]*$",
    r"[.](pem|key|p8|p12|pfx|keychain|keychain-db|kdbx)$",
    r"/(credentials|auth|secrets?)[.]json$",
    # Review R5 I-c: any OAuth token / client-secret file, not only the bare names —
    # `google_token.json`, `google_client_secret.json`, `client_secret_<id>.apps…json`,
    # `oauth-token.json`, `token.json.bak`. (`tokenizer.json` is not one: the word must
    # stand alone between `_ . -` separators.) No `(?:` — the sandbox profile reuses these.
    r"/([^/]*[_.-])?(tokens?|client_secrets?)([_.-][^/]*)?[.](json|pickle|pkl)([.~_-][^/]*)?$",
    r"/[.](netrc|npmrc|pypirc|git-credentials|pgpass)$",
    r"/prism-server[^/]*[.]db[^/]*$",
    r"/prism-config[.]json$",
    r"/pending-approvals[.]json$",
    r"/(Cookies|Login Data|cookies[.]sqlite|key[34][.]db|logins[.]json)[^/]*$",
    # What was typed at a prompt — tokens pasted into a command line live here for years.
    r"/[.](zsh|bash|sh|python|node_repl|psql|mysql|sqlite|irb|rediscli)_(history|sessions(/.*)?)$",
    r"/[.](lesshst|viminfo)$|/fish_history$",
)
#: …except the ones that are documentation, not secrets.
SECRET_NAME_EXCEPTIONS: Tuple[str, ...] = (
    r"/[.]env[.](example|sample|template|test|dist)$",
    # Work unblock 1 (audit 3.1): PUBLIC certificate bundles. curl / git / LibreSSL read the system
    # one, pip / requests certifi's, Homebrew's curl and openssl its own, brew its API signing key.
    # They hold no secret; without them no HTTPS works from the shell in any tier. Each is a
    # fixed place or a fixed name — a private key elsewhere (`key.pem`, `server.pem`) still matches.
    r"^(/private)?/etc/ssl/(cert[.]pem|certs/[^/]*)$",
    r"/certifi/cacert[.]pem$",
    r"^/(opt/homebrew|usr/local)/etc/(ca-certificates|openssl(@[0-9.]+)?)/cert[.]pem$",
    r"^/(opt/homebrew|usr/local)/Library/Homebrew/api/homebrew-[0-9]+[.]pem$",
    # Library SOURCE named after cookies (`http/cookies.py`, `requests/cookies.py`): imported by
    # every HTTP client. Only inside a language library tree; a browser's `Cookies` store is not one.
    r"/(lib/python[0-9.]*|site-packages|dist-packages|node_modules)/(.*/)?cookies[^/]*[.](py|pyc|pyi|js|cjs|mjs|ts)$",
)


def secret_dirs() -> List[str]:
    """Directories whose whole content is credentials or someone's private history."""
    h, hh, repo = HOME(), hermes_home(), repo_root()
    out = [
        h + "/.ssh", h + "/.gnupg", h + "/.aws", h + "/.azure", h + "/.kube", h + "/.docker",
        h + "/.config/gcloud", h + "/.config/gws", h + "/.config/gh", h + "/.config/gogcli", h + "/.config/op",
        h + "/Library/Keychains", h + "/Library/Cookies", h + "/Library/Application Support/gogcli",
        h + "/Library/Application Support/Google/Chrome", h + "/Library/Application Support/Firefox",
        # Omni's own signed-in Chrome (launchd com.example.omni-chrome; Hermes' browser.cdp_url).
        h + "/Library/Application Support/Omni Chrome",
        h + "/Library/Application Support/com.apple.TCC",
        h + "/.pm2", h + "/.parachute", h + "/parachute-backups", h + "/.codex", h + "/.claude/.credentials.json", h + "/.claude.json",
        h + "/.buzz", h + "/.prism",
        # Messages and Mail stores: other people's words, and a way to read login links (S3/B3).
        h + "/Library/Messages", h + "/Library/Mail",
        hh + "/auth.json", hh + "/config.yaml", hh + "/sessions", hh + "/state.db", hh + "/response_store.db",
        hh + "/pairing", hh + "/platforms", hh + "/gateway.sock", hh + "/cron/executions.db",
        # Omni's own signed-in Google session for joining meetings (google_meet storage_state).
        hh + "/workspace/meetings/auth.json",
        # ... and the Chrome profile it was signed in with (omni-meet googleauth: cookies, Keychain-encrypted).
        hh + "/workspace/meetings/omni-chrome-profile",
        # The repo's state/ is ordinary agent state (work unblock 5): only its credential files are
        # secret — state_secret() / state_secret_regex() — not the whole directory.
    ]
    out += _split(os.environ.get("OMNI_BRIDGE_SECRET_PATHS", ""))
    return sorted(set(real(p) for p in out))


#: Files under the repo's state/ that hold a credential or an approval record: the Bridge TLS pin
#: (proton-mail-state.json — deleting or truncating it makes the next sync trust whatever answers),
#: Matrix sync tokens, legacy approval drafts, Buzz identities (NIP-OA auth tags), and anything
#: named token / credential / secret / session / password. Any component under state/ counts.
STATE_CREDENTIAL = r"(proton-mail-state|matrix-sync-state|pending-approvals|identity|token|credential|secret|session|password|keychain)"
#: …and under state/ that nothing may WRITE: install receipts (a rollback restores from them).
STATE_GUARD = r"receipt"


def state_dir() -> str:
    return real(repo_root() + "/state")


def _sb_escape(text: str) -> str:
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", text)


def state_secret_regex() -> str:
    """The sandbox form of state_secret() (a regex on the real path)."""
    return "^%s/(.*/)?[^/]*%s[^/]*(/.*)?$" % (_sb_escape(state_dir()), STATE_CREDENTIAL)


def state_guard_regex() -> str:
    return "^%s/(.*/)?[^/]*%s[^/]*(/.*)?$" % (_sb_escape(state_dir()), STATE_GUARD)


def _under_state(path: str, pattern: str) -> bool:
    d = state_dir()
    if not path.startswith(d + "/"):
        return False
    return any(re.search(pattern, part, re.IGNORECASE) for part in path[len(d) + 1:].split("/"))


def state_secret(path: str) -> bool:
    return _under_state(path, STATE_CREDENTIAL)


_NAME_RE = None
_EXC_RE = None


def _bounded(pattern: str) -> str:
    """The Python form of a name pattern: every name part `[^/]*` capped at `[^/]{0,255}`
    (no file name is longer), so the token-name pattern cannot backtrack quadratically on a
    long word full of `_token_` (review R6 M-h). The sandbox profile keeps the unbounded
    form: macOS SBPL regex silently fails to match `{m,n}` (checked with sandbox-exec), and
    the kernel matcher does not backtrack."""
    return pattern.replace("[^/]*", "[^/]{0,255}")


def is_secret(path: str) -> bool:
    """`path` (already real) is, or is inside, something no Omni turn may read."""
    global _NAME_RE, _EXC_RE
    if _NAME_RE is None:
        _NAME_RE = re.compile("|".join("(?:%s)" % _bounded(p) for p in SECRET_NAME_PATTERNS), re.IGNORECASE)
        _EXC_RE = re.compile("|".join("(?:%s)" % p for p in SECRET_NAME_EXCEPTIONS), re.IGNORECASE)
    # Fix round 1 (I1): membership of a credential directory or a state credential comes FIRST —
    # a public-looking NAME (`certifi/cacert.pem`, `cookies.py`) never opens a file inside one.
    for d in secret_dirs():
        if path == d or path.startswith(d + "/"):
            return True
    if state_secret(path):
        return True
    if _NAME_RE.search(path) and not _EXC_RE.search(path):
        return True
    # Review R5 I-c: inside Hermes' home, ANY file whose name carries `token` or
    # `client_secret` as a word is a credential (google_token.json, a .bak of it, a pickle).
    hh = hermes_home()
    if path.startswith(hh + "/") and _HERMES_CRED_RE.search(os.path.basename(path)):
        return True
    return False


_HERMES_CRED_RE = re.compile(r"(^|[_.-])(tokens?|client_secrets?|refresh_token|credentials?)([_.-]|$)", re.IGNORECASE)


def holds_secret(path: str) -> bool:
    """`path` is a directory that CONTAINS a secret place (so reading it wholesale —
    `tar`, `cp -r`, `grep -r` — would sweep one up)."""
    if is_secret(path):
        return True
    base = path.rstrip("/") + "/"
    return any(d.startswith(base) for d in secret_dirs())


# ── where an Omni turn may write ─────────────────────────────────────────────

def workspace_roots() -> List[str]:
    """Writable without asking: the workspace(s), and the usual scratch places."""
    custom = os.environ.get("OMNI_BRIDGE_WORKSPACE", "")
    roots = _split(custom) if custom.strip() else [real("~/omni-workspace"), real(repo_root() + "/outputs")]
    return roots


def scratch_roots() -> List[str]:
    h = HOME()
    out = ["/private/tmp", "/tmp", "/private/var/folders", "/private/var/tmp", "/dev",
           h + "/Library/Caches", h + "/.cache", real(tempfile.gettempdir())]
    return sorted(set(real(p) if not p.startswith("/dev") else p for p in out))


def browser_profile_roots() -> List[str]:
    """Where the agent's own (signed-out to begin with) browser profiles live: `.browser-profiles`
    in each workspace. Chrome keeps `Cookies` / `Login Data` there, which the credential name
    patterns otherwise refuse; only a browser run (sandbox.profile(browser=True)) may open them."""
    return [r.rstrip("/") + "/.browser-profiles" for r in workspace_roots()]


def chrome_crashpad() -> str:
    """Google Chrome opens its crash database here whatever --user-data-dir says (audit 3.3).
    It sits inside the owner's Chrome profile directory, which stays a secret place."""
    return HOME() + "/Library/Application Support/Google/Chrome/Crashpad"


def default_workspace() -> str:
    return workspace_roots()[0]


def in_roots(path: str, roots: Iterable[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def writable(path: str) -> bool:
    return in_roots(path, workspace_roots()) or in_roots(path, scratch_roots())


# ── the guard itself ─────────────────────────────────────────────────────────

def guard_paths() -> List[str]:
    """What an Omni turn can never change, approved or not: this plugin, Hermes'
    settings and code, the files that start the services, and the secrets."""
    h, hh, repo = HOME(), hermes_home(), repo_root()
    out = [
        repo + "/hermes", repo + "/.env", repo + "/launchd", repo + "/.git/hooks", repo + "/.git/config", repo + "/.claude",
        repo + "/scripts", repo + "/venv", repo + "/config",
        # All of Hermes' home: settings, plugins, its own code, skills, memories and the cron
        # job file. Hermes' tools write there in-process (under their own rules); nothing
        # an Omni turn runs or writes by path may.
        hh,
        h + "/Library/LaunchAgents", "/Library/LaunchAgents", "/Library/LaunchDaemons",
        h + "/.zshrc", h + "/.zshenv", h + "/.zprofile", h + "/.bashrc", h + "/.bash_profile", h + "/.profile",
        h + "/.gitconfig", h + "/.config/git", h + "/.ssh", h + "/.pm2",
    ]
    out += trusted_binaries()
    out += python_site_dirs()
    out += python_prefixes()
    out += _split(os.environ.get("OMNI_BRIDGE_GUARD_PATHS", ""))
    return sorted(set(real(p) for p in out))


# ── skills Hermes' skill_manage may change ───────────────────────────────────

def local_skills_dir() -> str:
    """Hermes' own skills folder (~/.hermes/skills): the only place a skill_manage call may write."""
    try:
        from agent.skill_utils import get_skills_dir  # the gateway: Hermes' own answer
        return real(str(get_skills_dir()))
    except Exception:
        return real(os.path.join(hermes_home(), "skills"))


def skill_dirs() -> Tuple[List[str], bool]:
    """Every folder Hermes finds skills in, local first, then `skills.external_dirs` (Hermes' own resolution when it
    is importable, else config.yaml read the same way). The flag is False when the external list could not be read."""
    local = local_skills_dir()
    try:
        from agent.skill_utils import get_all_skills_dirs
        return [local] + [real(str(d)) for d in get_all_skills_dirs()[1:]], True
    except ImportError:
        pass
    except Exception:
        return [local], False
    try:
        import yaml
        cfg_path = os.path.join(hermes_home(), "config.yaml")
        if not os.path.exists(cfg_path):
            return [local], True
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        raw = ((cfg or {}).get("skills") or {}).get("external_dirs") if isinstance(cfg, dict) else None
        if isinstance(raw, str):
            raw = [raw]
        out = [local]
        for entry in raw or []:
            e = os.path.expanduser(os.path.expandvars(str(entry).strip()))
            if e:
                out.append(real(e if os.path.isabs(e) else os.path.join(hermes_home(), e)))
        return out, True
    except Exception:
        return [local], False


_mcp_names_cache: Tuple[float, Tuple[str, ...]] = (-1.0, ())


def mcp_server_names() -> Tuple[str, ...]:
    """The MCP servers configured in Hermes' config.yaml (`mcp_servers:` keys), longest first.

    Hermes names an MCP tool `mcp_<server>_<tool>` with both parts sanitized to [A-Za-z0-9_], so the
    name alone cannot say where the server ends; this list can. Agents cannot change it (config.yaml is a
    guarded path, manage_connections writes are refused), so a match against it is a real server, not one
    a tool picked a lookalike name for. Re-read when the file changes; empty when it cannot be read."""
    global _mcp_names_cache
    cfg_path = os.path.join(hermes_home(), "config.yaml")
    try:
        mtime = os.stat(cfg_path).st_mtime
    except OSError:
        return ()
    if mtime == _mcp_names_cache[0]:
        return _mcp_names_cache[1]
    try:
        with open(cfg_path, encoding="utf-8") as f:
            text = f.read()
        try:
            import yaml
            cfg = yaml.safe_load(text)
            servers = list(((cfg or {}).get("mcp_servers") or {}) if isinstance(cfg, dict) else {})
        except ImportError:
            # No PyYAML (a bare system python): the keys one indent under a top-level `mcp_servers:`.
            block = re.search(r"^mcp_servers:[ \t]*\n((?:[ \t]+.*\n|[ \t]*\n)*)", text + "\n", re.MULTILINE)
            indent = re.match(r"[ \t]+", block.group(1)) if block else None
            servers = re.findall(r"^%s([^\s#:][^:\n]*):" % re.escape(indent.group(0)), block.group(1), re.MULTILINE) \
                if indent else []
        names = tuple(sorted({re.sub(r"[^A-Za-z0-9_]", "_", str(k).strip().strip("'\"")) for k in servers if str(k).strip()},
                             key=len, reverse=True))
    except Exception:
        names = ()
    _mcp_names_cache = (mtime, names)
    return names


#: Folders Hermes' own skill scan never looks in (agent.skill_utils.EXCLUDED_SKILL_DIRS).
_SKILL_SCAN_SKIP = frozenset((".git", ".github", ".hub", ".archive", ".venv", "venv", "node_modules", "site-packages",
                              "__pycache__", ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache"))


def skill_manage_targets(args: dict) -> Tuple[List[str], bool]:
    """Real paths a skill_manage call would change, and whether they could be determined. `create` lands in the local
    folder (Hermes refuses a name that exists anywhere); every other action changes the skill Hermes finds by name in
    ANY skills folder, so every same-named skill counts (conservative: Hermes takes the first, which may be the repo's).
    A supporting `file_path` is resolved inside each, so a symlink out of a local skill is seen too."""
    action = str(args.get("action") or "").strip().lower()
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return [], True
    name = name.strip()
    dirs, complete = skill_dirs()
    if action == "create":
        category = args.get("category")
        base = os.path.join(dirs[0], str(category)) if isinstance(category, str) and category.strip() else dirs[0]
        found = [os.path.join(base, name)]
    else:
        found, seen, budget = [], set(), [20000]
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for root, subdirs, files in os.walk(d, followlinks=True):
                budget[0] -= 1
                if budget[0] < 0:
                    return [], False                # a tree too large to judge
                if os.path.basename(root) == name and "SKILL.md" in files:
                    found.append(root)
                key = real(root)
                if key in seen:                     # reached again through a link: a cycle, or already walked
                    subdirs[:] = []
                    continue
                seen.add(key)
                subdirs[:] = [x for x in subdirs if x not in _SKILL_SCAN_SKIP]
        if not found and not complete:
            return [], False  # Hermes may find it in an external folder this guard could not list
    fp = args.get("file_path")
    out = []
    for f in found:
        out.append(real(f))
        if isinstance(fp, str) and fp.strip():
            out.append(real(os.path.join(f, fp)))
    return out, True


def skill_write_protected(args: dict) -> bool:
    """Would this skill_manage call change a skill outside Hermes' local skills folder (The agent repo, a
    `skills.external_dirs` entry, or anything reached through a symlink into either)? Fails closed."""
    try:
        targets, known = skill_manage_targets(args if isinstance(args, dict) else {})
        if not known:
            return True
        local, repo = local_skills_dir(), repo_root()
        # Outside the local folder is an external_dirs skill (or a link out of a local one); real paths, so a symlink
        # into the repo resolves into the repo.
        return any(not in_roots(t, [local]) or in_roots(t, [repo]) for t in targets)
    except Exception:
        return True


_SITE_CACHE: dict = {}
_PREFIX_CACHE: dict = {}


def venv_base_prefix(venv_dir: str) -> Optional[str]:
    """The installation a virtualenv was made from: `home` in its pyvenv.cfg is the base interpreter's bin
    directory, so its parent is the base prefix (real path). None when `venv_dir` is not a venv."""
    try:
        with open(os.path.join(venv_dir, "pyvenv.cfg"), encoding="utf-8", errors="replace") as f:
            for line in f:
                k, sep, v = line.partition("=")
                if sep and k.strip().lower() == "home" and v.strip():
                    return os.path.dirname(real(v.strip().rstrip("/")))
    except OSError:
        pass
    return None


def _venv_home_link(venv_dir: str) -> Optional[str]:
    try:
        with open(os.path.join(venv_dir, "pyvenv.cfg"), encoding="utf-8", errors="replace") as f:
            for line in f:
                k, sep, v = line.partition("=")
                if sep and k.strip().lower() == "home" and v.strip():
                    return os.path.dirname(os.path.abspath(os.path.expanduser(v.strip().rstrip("/"))))
    except OSError:
        pass
    return None


def python_prefixes() -> List[str]:
    """Whole Python installations a credentialed run executes code from: the stdlib (`sitecustomize.py`,
    `encodings/`, a forged `__pycache__/*.pyc`) loads before the script's first line, exactly like site-packages.
    Guarded whole: the real prefix of every trusted interpreter, the BASE prefix of every venv among them (the
    repo's and Hermes' venvs run the base's stdlib; `pyvenv.cfg` `home`), `~/.local/share/uv/python` (both the
    `cpython-3.11-…` symlink and its `cpython-3.11.15-…` target), and Homebrew's python Cellar directories.
    A prefix that is root-owned all the way up is already safe; a shared prefix (/usr/local, /opt/homebrew, ~)
    is never guarded whole, only its `lib/python*` trees (see python_site_dirs). A workspace venv's base is
    readable, just not writable; the workspace venv itself is not touched."""
    import glob
    h = HOME()
    key = (tuple(trusted_binaries()), h, repo_root(), hermes_home())
    if key in _PREFIX_CACHE:
        return list(_PREFIX_CACHE[key])
    shared = {"/", "/usr", "/usr/local", "/opt", "/opt/homebrew", h, h + "/.local", h + "/.local/share"}
    out = [h + "/.local/share/uv/python"] + glob.glob("/opt/homebrew/Cellar/python@*") + glob.glob("/usr/local/Cellar/python@*")
    venvs = [repo_root() + "/venv", hermes_home() + "/hermes-agent/venv", hermes_home() + "/venv"]
    cands = []
    for d in trusted_binaries():
        for prog in sorted(glob.glob(d + "/python*")) + [d + "/hermes"]:
            if os.path.lexists(prog):
                cands.append(real(prog))
    cands += [real(v + "/bin/python3") for v in venvs if os.path.lexists(v + "/bin/python3")]
    pres = set()
    for c in cands:
        pre = os.path.dirname(os.path.dirname(c))
        pres.add(pre)
        base = venv_base_prefix(pre)
        if base:
            pres.add(base)
    for v in venvs:
        base = venv_base_prefix(v)
        if base:
            pres.add(base)
            # …and the name the venv's `home` gives it (a symlink such as cpython-3.11-… → cpython-3.11.15-…),
            # so the link itself cannot be swapped for another tree.
            link = _venv_home_link(v)
            if link and link not in shared and not _root_chain(link):
                out.append(link)
    for pre in pres:
        if pre not in shared and not _root_chain(pre):
            out.append(pre)
    if len(_PREFIX_CACHE) > 16:
        _PREFIX_CACHE.clear()
    _PREFIX_CACHE[key] = sorted(set(out))
    return list(_PREFIX_CACHE[key])


def python_site_dirs() -> List[str]:
    """Where a Python start-up hook (`usercustomize.py`, `sitecustomize.py`, a `.pth` line) would run inside every
    credentialed Python that loads that site: the user site directories (macOS and Linux layouts, kept whole so a
    missing one cannot be created), and `site-packages` of the prefix of every trusted interpreter / program
    (Homebrew, python.org, the repo's venv and Hermes' venv, found from the directories trusted_binaries() lists).
    A virtualenv in a workspace is not a trusted interpreter, so `pip install` there is unaffected."""
    import glob
    h = HOME()
    key = (tuple(trusted_binaries()), h, repo_root())
    if key not in _SITE_CACHE:
        out = [h + "/Library/Python", h + "/.local/lib"]
        prefixes = set()
        for d in trusted_binaries():
            prefixes.add(os.path.dirname(d))
            prefixes.add(os.path.dirname(real(d)))
        for prog in ("python3", "python", "hermes"):
            for d in trusted_binaries():
                cand = d + "/" + prog
                if os.path.lexists(cand):
                    prefixes.add(os.path.dirname(os.path.dirname(real(cand))))
        for pre in prefixes:
            if pre in ("/", "/usr", "/bin", "/sbin", "/System"):
                continue                       # root-owned already
            out += glob.glob(pre + "/lib/python*/site-packages") + glob.glob(pre + "/lib/python*/dist-packages")
            out += glob.glob(pre + "/Frameworks/Python.framework/Versions/*/lib/python*/site-packages")
        out += glob.glob(repo_root() + "/venv/lib/python*/site-packages")
        if len(_SITE_CACHE) > 16:
            _SITE_CACHE.clear()
        _SITE_CACHE[key] = sorted(set(out))
    return list(_SITE_CACHE[key])


#: Programs that run with a credential the sandbox grants them (readers, transports, the browser,
#: the agents a reader starts). Work unblock 11: overwrite one and its next run gets that credential.
TRUSTED_PROGRAMS = ("gog", "gws", "gh", "git", "ssh", "scp", "sftp", "rsync", "pm2", "agent-browser", "claude", "node",
                    "python3", "hermes",
                    # fix round 2: the filters a credentialed stream may pass through (Homebrew's jq, gnu tools)
                    "jq", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "column", "grep", "egrep", "fgrep", "sed",
                    "timeout", "gtimeout")
_BIN_CACHE: dict = {}


def trusted_binaries() -> List[str]:
    """The directories those programs are found in (Homebrew's and the user's bin directories) and,
    for each one, the directory its real binary sits in (`/opt/homebrew/bin/gog` →
    `…/Cellar/gogcli/0.25.0/bin`). System directories (/usr/bin, /bin) are read-only already."""
    import shutil
    h = HOME()
    bins = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", h + "/.local/bin"]
    search = os.pathsep.join([os.environ.get("PATH", "")] + bins)
    key = (search, h)
    if key not in _BIN_CACHE:
        found: List[str] = []
        static: List[str] = []
        hits = []
        for prog in TRUSTED_PROGRAMS:
            hits.append((shutil.which(prog, path=search), False))
            hits += [(d + "/" + prog, True) for d in bins if os.path.lexists(d + "/" + prog)]
        for where, from_bins in hits:
            if not where:
                continue
            for spot in (os.path.dirname(os.path.abspath(where)), os.path.dirname(real(where))):
                if spot.startswith(("/usr/", "/bin", "/sbin", "/System/")) and not spot.startswith("/usr/local/"):
                    continue                   # root-owned already
                found.append(spot)
                if from_bins:
                    static.append(spot)
        # fix round 2: the interpreter the repo's venv really runs (repo readers are run with it)
        venv_py = repo_root() + "/venv/bin/python"
        if os.path.lexists(venv_py):
            spot = os.path.dirname(real(venv_py))
            if not spot.startswith(("/usr/", "/bin", "/sbin", "/System/")) or spot.startswith("/usr/local/"):
                found.append(spot)
                static.append(spot)
        if len(_BIN_CACHE) > 16:
            _BIN_CACHE.clear()
        _BIN_CACHE[key] = (found, static)
    return bins + list(_BIN_CACHE[key][0])


def trusted_binary_dirs() -> List[str]:
    """Fix round 3: the subset of trusted_binaries() that may be TRUSTED, not only protected — the fixed
    bin directories and what the programs in them (and the repo's venv) resolve to. A directory found
    only because a program of that name came first on Hermes' PATH is protected (it is a guard path)
    but never trusted: the program there may have been planted before the guard looked."""
    trusted_binaries()
    h = HOME()
    bins = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", h + "/.local/bin"]
    key = (os.pathsep.join([os.environ.get("PATH", "")] + bins), h)
    return bins + list(_BIN_CACHE[key][1])


def _trust_guard(p: str) -> bool:
    """Is `p` inside a guard place that may be trusted (guard_paths() without the PATH-derived dirs)?"""
    derived = set(trusted_binaries()) - set(trusted_binary_dirs())
    roots = [g for g in guard_paths() if g not in derived]
    return in_roots(p, roots) or p in anchor_paths()


def is_guard(path: str) -> bool:
    return in_roots(path, guard_paths()) or _under_state(path, STATE_GUARD) or path in anchor_paths()


_ANCHOR_CACHE: dict = {}


def anchor_paths() -> List[str]:
    """Fix round 3 (R1): the ANCESTORS of every trusted binary directory that a process of this user
    could otherwise rename or replace (`~/.local` above `~/.local/bin`, `/opt/homebrew/Cellar/gogcli`
    above its `bin`). Each is protected as a single path — it cannot be renamed, moved, removed or
    replaced (sandbox `literal` deny, `self.files` in the policy) — while what is inside it stays
    writable (an install still creates files there). The walk stops at the first directory whose
    parent is root-owned and writable by nobody else: that one cannot be renamed by this user."""
    bins = trusted_binaries()
    # …and of the guard's own places: renaming the repo (`The agent repo`) or Hermes' home and
    # moving a look-alike tree into its place would turn every repo-script / venv grant over to it.
    h, hh, repo = HOME(), hermes_home(), repo_root()
    roots = list(bins) + [repo, repo + "/venv/bin", repo + "/scripts", hh] + python_prefixes() + _split(os.environ.get("OMNI_BRIDGE_GUARD_PATHS", ""))
    key = tuple(roots)
    if key in _ANCHOR_CACHE:
        return _ANCHOR_CACHE[key]
    out: List[str] = []
    for d in roots:
        for start in (os.path.abspath(d), real(d)):
            cur = start.rstrip("/") if start == repo else os.path.dirname(start.rstrip("/"))
            while cur and cur != "/":
                parent = os.path.dirname(cur)
                if _root_chain(parent):
                    break
                if cur not in out:
                    out.append(cur)
                cur = parent
    if len(_ANCHOR_CACHE) > 16:
        _ANCHOR_CACHE.clear()
    _ANCHOR_CACHE[key] = out
    return out


# ── fix round 2 (N1/N3): where a credentialed program may come from ─────────────────────────

_ROOT_CACHE: dict = {}


def _root_chain(path: str) -> bool:
    """`path` and every directory above it are owned by root and writable by nobody else (so no
    process of this user can replace or rename any of them)."""
    import stat as _stat
    p = real(path)
    if p in _ROOT_CACHE:
        return _ROOT_CACHE[p]
    ok = True
    cur = p
    try:
        while True:
            st = os.stat(cur)
            if st.st_uid != 0 or st.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH):
                ok = False
                break
            if cur == "/":
                break
            cur = os.path.dirname(cur)
    except OSError:
        ok = False
    if len(_ROOT_CACHE) > 4096:
        _ROOT_CACHE.clear()
    _ROOT_CACHE[p] = ok
    return ok


def unplantable(path: str) -> bool:
    """Nothing an Omni turn runs can write `path` (or what it is reached through): root-owned all the
    way up, or one of the guard's own paths (denied to every sandbox tier and every file tool)."""
    if not os.path.isabs(path):
        return False
    # Fix round 3 (R1): judged on the WHOLE chain, for the path as written and as resolved. The file
    # must be root-owned all the way up, or a guard path; then every directory above it, up to one
    # whose parent no process of this user can write, must itself be a guard / anchor path (which no
    # Omni turn can rename or replace). A guard path under a swappable parent is not enough: rename
    # the parent, put another tree in its place, and the "guarded" name points at a planted file.
    for p in {os.path.abspath(path), real(path)}:
        if not _chain_safe(p):
            return False
    return True


def _chain_safe(p: str) -> bool:
    try:
        if _root_chain(p):
            return True
        # A guard / anchor path is denied to every sandbox tier, inside the workspace or scratch too
        # (the guard deny comes after the allow), so `writable()` is not the question here.
        if not _trust_guard(p) or is_secret(p):
            return False
        cur = p
        while cur != "/":
            parent = os.path.dirname(cur)
            if _root_chain(parent):
                return True                        # cur cannot be renamed: its parent is root's alone
            # its parent is this user's: cur stays put only if cur itself is a guard / anchor path
            if not is_guard(cur):
                return False
            cur = parent
        return True
    except Exception:
        return False                               # a check that cannot be made fails closed


def trusted_path() -> str:
    """The PATH a credentialed command runs with (sandbox.wrap): Hermes' own PATH minus every directory
    that does not exist or that an Omni turn could write (`~/.cargo/bin`, `~/.npm-global/bin` before
    Homebrew's on the agent host) — so a bare `gog` / `jq` / `head` cannot be shadowed by a planted program."""
    key = tuple(os.environ.get(k, "") for k in ("PATH", "HOME", "HERMES_HOME", "OMNI_BRIDGE_GUARD_PATHS", "OMNI_BRIDGE_WORKSPACE",
                                                   "OMNI_BRIDGE_SECRET_PATHS"))
    if key in _TP_CACHE and time.monotonic() - _TP_CACHE[key][0] < 30:
        return _TP_CACHE[key][1]
    out = []
    for d in os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(":"):
        if d and os.path.isabs(d) and os.path.isdir(d) and unplantable(d) and d not in out:
            out.append(d)
    value = ":".join(out) or "/usr/bin:/bin:/usr/sbin:/sbin"
    if len(_TP_CACHE) > 32:
        _TP_CACHE.clear()
    _TP_CACHE[key] = (time.monotonic(), value)
    return value


_TP_CACHE: dict = {}
