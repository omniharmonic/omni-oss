"""Known secret VALUES, scrubbed from anything an Omni turn's tools return.

The sandbox and the path rules keep secrets from being read. This is the third
layer, for the paths nobody listed: if a credential's exact value still turns
up in a tool's output — a log line, a search hit, a process list — it is
replaced before the model sees it, and a tool call that carries one is refused.

Which values count
------------------
An env file holds far more configuration than credentials: an email address, a
chat id, a host, a port, a path, `true`. Treating those as secrets would refuse
every command that mentions the owner's own address. So a value is a secret
only when `classify(name, value)` says so:

1. its NAME says credential — one of its `_`-separated words is KEY, SECRET,
   TOKEN, PASSWORD, HASH, PRIVATE, CREDENTIAL, … (the M0 rule: `*_KEY`,
   `*_SECRET`, `*_TOKEN`, `*_PASSWORD`, `*_HASH`) — and the name does not end
   in a word that says "where" or "which" (…_FILE, …_PATH, …_URL, …_NAME,
   …_HEADER, …_ID, …_USER, …_EMAIL, …_HOST, …_PORT, …_ENABLED, …_TTL …), and
   its value is at least 8 characters
   (known label fields `cache_key`, `sort_key` and `auth` may hold plain
   lower-case words such as `sessions-by-chat` rather than credentials); or
2. whatever it is called, the value has the SHAPE of a credential: a known
   token format (`sk-…`, `ghp_…`, `xoxb-…`, `AKIA…`, a JWT, a PEM block), a
   URL with a password in it (the password is the secret), or 32+ characters
   of hex / base64 with both letters and digits — the last only when the
   name does not say "address" or "identifier" (…_ADDRESS, …_ID, …_SHA …).

Ordinary names holding an email address, number, boolean, path, URL or host
are configuration. Credential names still protect such values: a password may
be numeric or look like a host. Values under 8 characters are not scrubbed.

`OMNI_BRIDGE_SECRET_KEYS` / `OMNI_BRIDGE_NOT_SECRET_KEYS` (comma lists of
names) settle a name by hand. `explain()` — `scripts/omni_policy_check.py
--explain-secrets` — lists, per file, the NAMES whose values are treated as
secrets, never a value.

Values come from `<HERMES_HOME>/.env`, the agent repo's `.env`,
`OMNI_BRIDGE_SECRET_FILES`, Hermes' `config.yaml` and `auth.json`, and this
process's own environment. They are held in memory only, never logged, and
re-read when a file changes.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple

from . import paths

MIN_NAMED = 8          # a credential-named value shorter than this is not scrubbed
PLACEHOLDER = "[redacted by omni-bridge]"

#: Words in a NAME that say "this is a credential".
_CRED_WORDS = frozenset({
    "KEY", "APIKEY", "SECRET", "SECRETS", "TOKEN", "PASSWORD", "PASSWD", "PASSPHRASE", "PASS", "PWD", "HASH", "PRIVATE",
    "CREDENTIAL", "CREDENTIALS", "COOKIE", "BEARER", "AUTH", "SIGNATURE", "SALT", "SEED", "MNEMONIC", "PAT", "DSN",
})
#: Words that say "credential" only weakly: `key` is also a cache key, a sort key, a map key.
#: Only known label fields use this exception; API_KEY and SESSION_KEY remain credentials.
_WEAK_WORDS = frozenset({"KEY", "AUTH", "HASH", "SALT", "SEED", "PAT", "PASS"})
#: A NAME ending in one of these says where a credential is or which one — not the credential.
_WHERE_WORDS = frozenset({
    "FILE", "FILES", "PATH", "PATHS", "DIR", "DIRECTORY", "URL", "URI", "ENDPOINT", "NAME", "HEADER", "PREFIX", "ID", "IDS",
    "USER", "USERNAME", "EMAIL", "ADDRESS", "HOST", "HOSTNAME", "PORT", "ENABLED", "DISABLED", "REQUIRED", "TTL", "MS", "S",
    "SECONDS", "TIMEOUT", "LENGTH", "SIZE", "COUNT", "LIMIT", "TYPE", "KIND", "MODE", "SERVICE", "ACCOUNT", "ALGORITHM", "ALG",
    "SCOPE", "SCOPES", "VERSION", "EXPIRY", "EXPIRES", "LIFETIME", "ROTATION", "LABEL",
})
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.*?)\s*$")
_PAIR = re.compile(r"""^\s*-?\s*["']?([A-Za-z_][A-Za-z0-9_.-]*)["']?\s*[:=]\s*["']?([^"'\s#,}]{8,})""")
_BEARER = re.compile(r"\b(?:Bearer|Basic)\s+([A-Za-z0-9_\-./+=]{16,})")
_TOKEN_SHAPES = re.compile(
    r"^(sk-[A-Za-z0-9_-]{16,}|sk_(live|test)_[A-Za-z0-9]{16,}|rk_(live|test)_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|ya29\.[A-Za-z0-9_.-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|\d{6,12}:[A-Za-z0-9_-]{30,}|pd_[A-Za-z0-9_-]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*)$", re.DOTALL)
_URL_PASSWORD = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]*:([^@/\s]{4,})@")

_cache: Dict[str, Tuple[float, List[Tuple[str, str]]]] = {}


def _words(name: str) -> List[str]:
    # API_SERVER_KEY, apiKey, api-key, oauth.token → words, upper-cased
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.upper()) if w]


def _listed(setting: str) -> frozenset:
    return frozenset(x.strip().upper() for x in os.environ.get(setting, "").split(",") if x.strip())


def ordinary(value: str) -> bool:
    """Plainly configuration, never a credential: an email address, a number, a boolean,
    a path, a URL with no password in it, a host name."""
    v = value.strip()
    if len(v) < MIN_NAMED:
        return True
    if re.match(r"^(true|false|yes|no|on|off|none|null|enabled|disabled)$", v, re.IGNORECASE):
        return True
    if re.match(r"^[+-]?[\d.,:_ -]+$", v):                                   # numbers, ids, ports, times
        return True
    if re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$", v):   # an email address
        return True
    if re.match(r"^(~|\.{0,2}/|[A-Za-z]:\\)", v):                             # a path
        return True
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", v):                           # a URL: only its password, if any
        return True
    if re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,24}(:\d+)?$", v):       # a host name, lower case (maybe with a port)
        return True
    return False


#: A NAME ending in one of these holds an identifier that is long and random-looking by nature.
_IDENTIFIER_WORDS = frozenset({"SHA", "SHA1", "SHA256", "COMMIT", "DIGEST", "CHECKSUM", "REVISION", "REV", "BUILD", "UUID", "GUID", "TAG"})


def shaped(value: str, anonymous: bool = True) -> Optional[str]:
    """The part of `value` that has the shape of a credential whatever it is called, or None.
    `anonymous=False` leaves out the "long and random-looking" rule (for a name that says
    the value is an address or an identifier)."""
    v = value.strip()
    m = _URL_PASSWORD.match(v)
    if m:
        return m.group(1) if len(m.group(1)) >= MIN_NAMED else None
    if _TOKEN_SHAPES.match(v):
        return v
    if anonymous and len(v) >= 32 and re.match(r"^[A-Za-z0-9+/=_-]+$", v) and re.search(r"[A-Za-z]", v) and re.search(r"\d", v) and not ordinary(v):
        return v
    return None


def _on_disk(v: str) -> bool:
    try:
        return os.path.lexists(v)
    except (OSError, ValueError):
        return False


def classify(name: str, value: str) -> Optional[str]:
    """The secret in `name = value`, or None. See the module docstring."""
    if not isinstance(value, str):
        return None
    v = value.strip().strip("'\"")
    if not v:
        return None
    up = (name or "").upper()
    if up in _listed("OMNI_BRIDGE_NOT_SECRET_KEYS"):
        return None
    if up in _listed("OMNI_BRIDGE_SECRET_KEYS"):
        return v if len(v) >= MIN_NAMED else None
    words = _words(name or "")
    by_shape = shaped(v, anonymous=not (words and words[-1] in (_WHERE_WORDS | _IDENTIFIER_WORDS)))
    if by_shape:
        return by_shape
    if v.startswith("/") and _on_disk(v):
        return None          # `PWD`, `SSH_AUTH_SOCK`, `*_KEY_FILE`: where something is, not the credential itself
    named = _CRED_WORDS & set(words)
    if not named or words[-1] in _WHERE_WORDS or len(v) < MIN_NAMED:
        return None
    label_field = words[-2:] in (["CACHE", "KEY"], ["SORT", "KEY"]) or words == ["AUTH"]
    if label_field and not (named - _WEAK_WORDS) and re.match(r"^[a-z]+([-_ .:/][a-z]+)+$", v):
        return None          # `cache_key: sessions-by-chat`, `sort_key: created-at`: a label, not a credential
    return v


def _files() -> List[str]:
    out = [paths.hermes_home() + "/.env", paths.repo_root() + "/.env", paths.hermes_home() + "/config.yaml",
           paths.hermes_home() + "/auth.json"]
    out += [paths.real(p) for p in os.environ.get("OMNI_BRIDGE_SECRET_FILES", "").split(":") if p.strip()]
    return list(dict.fromkeys(out))


def _pairs(path: str, text: str) -> List[Tuple[str, str]]:
    """(name, value) for every assignment in a file: `NAME=value` in an env file,
    `name: value` in YAML / JSON."""
    out: List[Tuple[str, str]] = []
    env_like = os.path.basename(path).startswith(".env") or path.endswith(".env")
    if path.endswith(".json") and not env_like:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = None
        if data is not None:
            def walk(node, prefix=""):
                if isinstance(node, dict):
                    for key, value in node.items():
                        walk(value, prefix + "." + str(key) if prefix else str(key))
                elif isinstance(node, list):
                    for value in node:
                        walk(value, prefix)
                elif isinstance(node, str):
                    out.append((prefix, node))
                    for bearer in _BEARER.finditer(node):
                        out.append(("authorization_token", bearer.group(1)))
            walk(data)
            return out
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ASSIGN.match(line) if env_like else None
        if m:
            out.append((m.group(1), m.group(2)))
            continue
        if not env_like:
            m = _PAIR.match(line)
            if m:
                out.append((m.group(1), m.group(2)))
            for b in _BEARER.finditer(line):
                out.append(("authorization_token", b.group(1)))
    return out


def _from_file(path: str) -> List[Tuple[str, str]]:
    """(name, secret) for the values of `path` that are secrets."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = "%s\0%s\0%s" % (path, os.environ.get("OMNI_BRIDGE_SECRET_KEYS", ""), os.environ.get("OMNI_BRIDGE_NOT_SECRET_KEYS", ""))
    hit = _cache.get(key)
    if hit and hit[0] == st.st_mtime:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read(2_000_000)
    except OSError:
        return []
    found: List[Tuple[str, str]] = []
    for name, value in _pairs(path, text):
        secret = classify(name, value)
        if secret:
            found.append((name, secret))
    if len(_cache) > 64:
        _cache.clear()
    _cache[key] = (st.st_mtime, found)
    return found


def _from_environment() -> List[Tuple[str, str]]:
    out = []
    for k, v in os.environ.items():
        secret = classify(k, v)
        if secret:
            out.append((k, secret))
    return out


def values() -> List[str]:
    out = set()
    for p in _files():
        out.update(v for _n, v in _from_file(p))
    out.update(v for _n, v in _from_environment())
    # Longest first, so a value that contains another is replaced whole.
    return sorted(out, key=len, reverse=True)


def explain() -> List[Dict[str, object]]:
    """Per source: how many of its values are treated as secrets, and their NAMES.
    Never a value. For a person checking a machine."""
    report: List[Dict[str, object]] = []
    for p in _files():
        if not os.path.exists(p):
            report.append({"source": p, "exists": False, "assignments": 0, "secrets": []})
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                total = len(_pairs(p, f.read(2_000_000)))
        except OSError:
            report.append({"source": p, "exists": True, "readable": False, "assignments": 0, "secrets": []})
            continue
        report.append({"source": p, "exists": True, "assignments": total, "secrets": sorted({n for n, _v in _from_file(p)})})
    env = _from_environment()
    report.append({"source": "this process's environment", "exists": True, "assignments": len(os.environ), "secrets": sorted({n for n, _v in env})})
    return report


def contains(text: str) -> bool:
    if not text:
        return False
    return any(v in text for v in values())


def scrub(text: str) -> str:
    if not text:
        return text
    for v in values():
        if v in text:
            text = text.replace(v, PLACEHOLDER)
    return text
