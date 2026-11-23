"""A conservative reader of shell command lines, for the Omni tool policy.

It answers one question — "which programs would this line run, with which
arguments?" — well enough to route a command (run it contained, ask the owner,
or refuse), and it says so honestly when it cannot tell (`Parsed.opaque`).

It is NOT the enforcement. Every Omni shell command runs inside an OS sandbox
(sandbox.py) whatever this parser concluded, so a construct it misreads is
still contained: no secret file, no network beyond this machine, no write
outside the workspace. The parser only decides whether a command gets MORE
than that (a trusted reader's credentials, an approval card) or less (a
refusal with a clear message).

What it understands: quoting ('…', "…", $'…', backslash), `;` `&&` `||` `|`
`&` and newlines, redirections, here-documents, `$( … )`, backticks and
process substitution (read recursively), leading `VAR=value` assignments, and
the wrappers that run another command: `sh|bash|zsh -c`, `env`, `sudo`,
`nohup`, `time`, `nice`, `command`, `exec`, `xargs`, `find -exec`, `eval`
with a literal string, and inline code for python / node / perl / ruby /
osascript (`-c` / `-e`), whose text is kept for scanning.

What makes a line `opaque`: unbalanced quotes or brackets, a variable or a
glob as the program name, `eval` / `source` of something that is not a
literal, a script fed on stdin (`… | sh`), shell functions, and anything
else it does not model. Opaque is not "dangerous" — it is "cannot vouch".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

__all__ = ["Simple", "Parsed", "parse", "basename"]

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
_INLINE = {
    # program → the flags whose next argument is code
    "python": ("-c",), "python3": ("-c",), "python2": ("-c",),
    "node": ("-e", "--eval", "-p", "--print"), "nodejs": ("-e", "--eval", "-p", "--print"),
    "deno": ("eval",), "bun": ("-e", "--eval"),
    "perl": ("-e", "-E"), "ruby": ("-e",), "php": ("-r",),
    "osascript": ("-e",), "lua": ("-e",), "awk": (), "gawk": (),
}
# Wrappers: run the command that follows their own options.
_PREFIX_WRAPPERS = frozenset({"nohup", "time", "command", "builtin", "exec", "nice", "ionice",
                              "caffeinate", "stdbuf", "unbuffer", "setsid", "arch", "chronic"})
_KEYWORDS = frozenset({"if", "then", "elif", "else", "fi", "for", "select", "while", "until",
                       "do", "done", "case", "esac", "in", "{", "}", "!", "[[", "]]", "function", "coproc"})
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_PYTHON = re.compile(r"^(python|pypy)[0-9.]*$")


def basename(arg: str) -> str:
    return os.path.basename(arg.rstrip("/")) if arg else ""


@dataclass
class Simple:
    """One program invocation the line would make."""
    argv: List[str]
    #: `VAR=value` words that preceded it.
    assigns: Dict[str, str] = field(default_factory=dict)
    #: (operator, target) — e.g. (">", "out.txt"), ("<", ".env").
    redirects: List[Tuple[str, str]] = field(default_factory=list)
    #: How it is reached: "" for the line itself, else "sh -c", "$( )", "xargs", …
    via: str = ""
    #: Inline program text (`python -c …`, a here-document fed to an interpreter).
    code: Optional[str] = None
    #: Language of `code` ("python", "shell", "node", …).
    lang: Optional[str] = None
    #: Words came from somewhere the parser cannot see (xargs' stdin, a glob).
    dynamic: bool = False
    #: It reads its program from standard input (`… | sh`, `python -`).
    stdin_script: bool = False

    @property
    def prog(self) -> str:
        return basename(self.argv[0]) if self.argv else ""


@dataclass
class Parsed:
    commands: List[Simple] = field(default_factory=list)
    #: Why the line cannot be fully vouched for (empty = fully understood).
    opaque: List[str] = field(default_factory=list)
    #: The line is exactly one program invocation: no pipe, list, redirection,
    #: substitution or wrapper (optionally after `cd <dir> &&`).
    standalone: bool = False
    #: The `cd` target when the line starts with `cd <dir> &&`.
    cd: Optional[str] = None
    #: Inner command lines of `$( … )` / backticks inside an otherwise standalone line.
    substitutions: List[str] = field(default_factory=list)
    #: The line's top-level commands in order, each with what it unwraps to (`timeout 5 gog …` → [gog]),
    #: and the operators between them — for policy._grant_head (a reader piped into filters).
    stages: List[List[Simple]] = field(default_factory=list)
    #: The top-level commands as written (before a wrapper is followed), aligned with `stages`.
    raws: List[Simple] = field(default_factory=list)
    separators: List[str] = field(default_factory=list)

    def note(self, why: str) -> None:
        if why not in self.opaque:
            self.opaque.append(why)


class _Unbalanced(Exception):
    pass


# ── tokenizer ────────────────────────────────────────────────────────────────

_OPS = ("&&", "||", "|&", ";;", ";", "|", "&", "\n", "(", ")")
_REDIR = re.compile(r"^(\d*)(>>|>\||>&|<&|<<<|<<-|<<|<>|>|<|&>>|&>)")


@dataclass
class _Tok:
    kind: str            # "word" | "op" | "redir"
    text: str            # the word's value (quotes removed), the operator, or the redirection operator
    #: For a word: False when any part of it was unquoted `$var`, a glob or `~user`.
    literal: bool = True
    #: For a word: it contained a `$( )`/backtick substitution.
    subst: bool = False
    #: Raw source text.
    raw: str = ""


def _ansi_c(body: str) -> Optional[str]:
    """Decode a $'…' string; None when it uses an escape this does not model."""
    out, i = [], 0
    simple = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "a": "\a", "b": "\b",
              "e": "\x1b", "E": "\x1b", "f": "\f", "v": "\v", "0": "\0"}
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= len(body):
            return None
        e = body[i]
        m = re.match(r"x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|([0-7]{1,3})", body[i:])
        if m and not (e == "0" and len(m.group(0)) == 1):
            digits = m.group(1) or m.group(2) or m.group(3)
            try:
                out.append(chr(int(digits, 16)) if digits else chr(int(m.group(4), 8)))
            except (ValueError, OverflowError):
                return None
            i += len(m.group(0))
        elif e in simple:
            out.append(simple[e])
            i += 1
        else:
            return None
    return "".join(out)


def _matching(src: str, i: int, open_c: str, close_c: str) -> int:
    """Index just past the bracket that closes the one at src[i-1]. Quote-aware."""
    depth = 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = src.find("'", i + 1)
            if j < 0:
                raise _Unbalanced("quote")
            i = j + 1
            continue
        if c == '"':
            i += 1
            while i < len(src) and src[i] != '"':
                i += 2 if src[i] == "\\" else 1
            if i >= len(src):
                raise _Unbalanced("quote")
            i += 1
            continue
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise _Unbalanced("bracket")


def _tokenize(src: str, out: Parsed, inner: List[str], heredocs: List[Tuple[str, str]]) -> List[_Tok]:
    """Words, operators and redirections. Substituted commands go to `inner`,
    here-document bodies to `heredocs` as (delimiter, body)."""
    toks: List[_Tok] = []
    i, n = 0, len(src)
    pending: List[Tuple[str, bool]] = []      # here-doc delimiters awaiting their bodies
    cur: List[str] = []
    cur_raw_start = -1
    literal, subst, have = True, False, False

    def flush(end: int) -> None:
        nonlocal cur, literal, subst, have, cur_raw_start
        if have:
            toks.append(_Tok("word", "".join(cur), literal, subst, src[cur_raw_start:end]))
        cur, literal, subst, have, cur_raw_start = [], True, False, False, -1

    def start(pos: int) -> None:
        nonlocal have, cur_raw_start
        if not have:
            have, cur_raw_start = True, pos

    while i < n:
        c = src[i]
        if c == "\n":
            flush(i)
            toks.append(_Tok("op", "\n"))
            i += 1
            # Here-document bodies follow the line that announced them.
            while pending:
                delim, strip = pending.pop(0)
                lines = []
                while i <= n:
                    j = src.find("\n", i)
                    line = src[i:] if j < 0 else src[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
                    lines.append(line)
                    if j < 0:
                        out.note("a here-document without its end")
                        break
                heredocs.append((delim, "\n".join(lines)))
            continue
        if c in " \t":
            flush(i)
            i += 1
            continue
        if c == "#" and not have:
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "\\":
            if i + 1 < n and src[i + 1] == "\n":
                i += 2
                continue
            start(i)
            cur.append(src[i + 1] if i + 1 < n else "")
            i += 2
            continue
        if c == "'":
            j = src.find("'", i + 1)
            if j < 0:
                raise _Unbalanced("quote")
            start(i)
            cur.append(src[i + 1:j])
            i = j + 1
            continue
        if c == "$" and i + 1 < n and src[i + 1] == "'":
            j = i + 2
            while j < n and src[j] != "'":
                j += 2 if src[j] == "\\" else 1
            if j >= n:
                raise _Unbalanced("quote")
            decoded = _ansi_c(src[i + 2:j])
            start(i)
            if decoded is None:
                literal = False
                out.note("an escape sequence in $'…' this reader does not decode")
                cur.append(src[i + 2:j])
            else:
                cur.append(decoded)
            i = j + 1
            continue
        if c == '"':
            start(i)
            i += 1
            while True:
                if i >= n:
                    raise _Unbalanced("quote")
                d = src[i]
                if d == '"':
                    i += 1
                    break
                if d == "\\" and i + 1 < n and src[i + 1] in '$`"\\\n':
                    if src[i + 1] != "\n":
                        cur.append(src[i + 1])
                    i += 2
                    continue
                if d == "`":
                    j = src.find("`", i + 1)
                    if j < 0:
                        raise _Unbalanced("backtick")
                    inner.append(src[i + 1:j])
                    subst = True
                    cur.append("\0SUBST\0")
                    i = j + 1
                    continue
                if d == "$" and i + 1 < n and src[i + 1] == "(":
                    if i + 2 < n and src[i + 2] == "(":
                        j = _matching(src, i + 3, "(", ")")
                        j = j + 1 if j < n and src[j] == ")" else j
                        cur.append("\0ARITH\0")
                        literal = False
                        i = j
                        continue
                    j = _matching(src, i + 2, "(", ")")
                    inner.append(src[i + 2:j - 1])
                    subst = True
                    cur.append("\0SUBST\0")
                    i = j
                    continue
                if d == "$" and i + 1 < n and (src[i + 1].isalnum() or src[i + 1] in "_{@*#?!$-"):
                    literal = False
                    m = re.match(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$.", src[i:])
                    cur.append(m.group(0) if m else "$")
                    i += len(m.group(0)) if m else 1
                    continue
                cur.append(d)
                i += 1
            continue
        if c == "`":
            j = src.find("`", i + 1)
            if j < 0:
                raise _Unbalanced("backtick")
            start(i)
            inner.append(src[i + 1:j])
            subst = True
            cur.append("\0SUBST\0")
            i = j + 1
            continue
        if c == "$" and i + 1 < n and src[i + 1] == "(":
            start(i)
            if i + 2 < n and src[i + 2] == "(":
                j = _matching(src, i + 3, "(", ")")
                j = j + 1 if j < n and src[j] == ")" else j
                literal = False
                cur.append("\0ARITH\0")
                i = j
                continue
            j = _matching(src, i + 2, "(", ")")
            inner.append(src[i + 2:j - 1])
            subst = True
            cur.append("\0SUBST\0")
            i = j
            continue
        if c in "<>" and i + 1 < n and src[i + 1] == "(":
            # Process substitution: <(cmd) / >(cmd).
            start(i)
            j = _matching(src, i + 2, "(", ")")
            inner.append(src[i + 2:j - 1])
            subst = True
            cur.append("\0SUBST\0")
            i = j
            continue
        if c == "$":
            start(i)
            m = re.match(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9@*#?!$-]", src[i:])
            if m:
                literal = False
                cur.append(m.group(0))
                i += len(m.group(0))
            else:
                cur.append("$")
                i += 1
            continue
        # A redirection (possibly with a leading fd digit glued on).
        m = _REDIR.match(src[i:]) if (c in "<>&" or (c.isdigit() and not have)) else None
        if m and (c in "<>" or m.group(2).startswith("&>") or m.group(1)):
            if c == "&" and not m.group(2).startswith("&>"):
                m = None
        if m:
            flush(i)
            op = m.group(2)
            i += len(m.group(0))
            if op in ("<<", "<<-"):
                # The delimiter word: quoted or not, taken literally.
                while i < n and src[i] in " \t":
                    i += 1
                dm = re.match(r"'([^']*)'|\"([^\"]*)\"|\\?([^\s;&|<>()]+)", src[i:])
                if not dm:
                    out.note("a here-document without a delimiter")
                    continue
                delim = dm.group(1) if dm.group(1) is not None else dm.group(2) if dm.group(2) is not None else dm.group(3)
                i += len(dm.group(0))
                pending.append((delim, op == "<<-"))
                toks.append(_Tok("redir", "<<"))
                toks.append(_Tok("word", delim))
            else:
                toks.append(_Tok("redir", op))
            continue
        matched = False
        for op in _OPS:
            if src.startswith(op, i):
                flush(i)
                toks.append(_Tok("op", op))
                i += len(op)
                matched = True
                break
        if matched:
            continue
        if c in "*?[" or (c == "~" and not have) or (c == "{" and "," in src[i:src.find("}", i) if src.find("}", i) > 0 else i]):
            start(i)
            literal = literal and c == "~" and (i + 1 >= n or src[i + 1] in "/ \t\n;&|")
            cur.append(c)
            i += 1
            continue
        start(i)
        cur.append(c)
        i += 1
    flush(n)
    for delim, _ in pending:
        out.note("a here-document without its body")
        heredocs.append((delim, ""))
    return toks


# ── grammar: lists of simple commands, wrappers unwrapped ─────────────────────

def _expand_home(word: str) -> str:
    return os.path.expanduser(word) if word.startswith("~") else word


def _take_options(argv: List[str], with_value: Tuple[str, ...] = ()) -> int:
    """Index of the first word that is not an option of the wrapper at argv[0]."""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return i + 1
        if not a.startswith("-") or a == "-":
            return i
        if a in with_value or (len(a) == 2 and a in with_value):
            i += 2
        else:
            i += 1
    return i


def _unwrap(cmd: Simple, out: Parsed, depth: int) -> List[Simple]:
    """Follow a wrapper to the command(s) it runs. Returns the commands to judge:
    the wrapper itself is kept too when it matters by name (sudo, xargs, find)."""
    if depth > 8:
        out.note("commands nested too deeply to follow")
        return [cmd]
    argv = cmd.argv
    if not argv:
        return [cmd]
    prog = cmd.prog
    via = cmd.via

    def sub(rest: List[str], how: str, dynamic: bool = False) -> List[Simple]:
        if not rest:
            return []
        inner = Simple(argv=rest, redirects=list(cmd.redirects), via=(via + " → " + how).strip(" →"), dynamic=dynamic or cmd.dynamic)
        return _unwrap(inner, out, depth + 1)

    if prog in _PREFIX_WRAPPERS:
        skip = _take_options(argv, ("-n", "-c", "-p", "-o", "-i", "-e", "-t", "-w", "-a", "-arch"))
        return sub(argv[skip:], prog)
    if prog in ("timeout", "gtimeout"):
        # `timeout [-s SIG] [-k DUR] [--foreground|--preserve-status|-v] DURATION cmd …` (round 5).
        skip = _take_options(argv, ("-s", "-k", "--signal", "--kill-after"))
        return sub(argv[skip + 1:], prog) if len(argv) > skip + 1 else [cmd]
    if prog == "env":
        i = 1
        while i < len(argv):
            a = argv[i]
            if a in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-P"):
                if a in ("-S", "--split-string") and i + 1 < len(argv):
                    inner = parse(argv[i + 1], _depth=depth + 1)
                    out.opaque.extend(x for x in inner.opaque if x not in out.opaque)
                    for c in inner.commands:
                        c.via = (via + " → env -S").strip(" →")
                    return [cmd] + inner.commands
                i += 2
            elif a.startswith("-") and a != "-":
                i += 1
            elif _ASSIGN.match(a):
                i += 1
            else:
                break
        rest = argv[i:]
        return sub(rest, "env") if rest else [cmd]
    if prog in ("sudo", "doas"):
        skip = _take_options(argv, ("-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t"))
        return [cmd] + sub(argv[skip:], prog)
    if prog == "xargs":
        skip = _take_options(argv, ("-I", "-n", "-P", "-L", "-s", "-E", "-d", "-a", "-J", "-R", "-S"))
        rest = argv[skip:] or ["echo"]
        return [cmd] + sub(rest, "xargs", dynamic=True)
    if prog in ("find", "gfind", "fd"):
        found: List[Simple] = [cmd]
        i = 1
        while i < len(argv):
            if argv[i] in ("-exec", "-execdir", "-ok", "-okdir", "-x", "--exec", "-X", "--exec-batch"):
                j = i + 1
                while j < len(argv) and argv[j] not in (";", "+", "\\;"):
                    j += 1
                found += sub([a for a in argv[i + 1:j] if a != "{}"] or ["true"], "find -exec", dynamic=True)
                i = j
            i += 1
        return found
    if prog in _SHELLS:
        i = 1
        script = None
        while i < len(argv):
            a = argv[i]
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                script = argv[i + 1] if i + 1 < len(argv) else ""
                break
            if a in ("-o", "+o", "--rcfile", "--init-file"):
                i += 2
                continue
            if a.startswith("-") or a.startswith("+"):
                i += 1
                continue
            break
        if script is not None:
            inner = parse(script, _depth=depth + 1)
            out.opaque.extend(x for x in inner.opaque if x not in out.opaque)
            for c in inner.commands:
                c.via = (via + " → " + prog + " -c → " + c.via).strip(" →")
            return [cmd] + inner.commands
        if i >= len(argv) or argv[i] == "-s":
            cmd.stdin_script = True
            cmd.lang = "shell"
        return [cmd]
    if prog == "eval":
        text = " ".join(argv[1:])
        if "\0" in text or "$" in text:
            out.note("eval of text that is only known when it runs")
            return [cmd]
        inner = parse(text, _depth=depth + 1)
        out.opaque.extend(x for x in inner.opaque if x not in out.opaque)
        for c in inner.commands:
            c.via = (via + " → eval → " + c.via).strip(" →")
        return [cmd] + inner.commands
    if prog in ("source", "."):
        out.note("a sourced file: its commands are not read here")
        return [cmd]
    key = "python" if _PYTHON.match(prog) else prog
    if key in _INLINE:
        flags = _INLINE[key]
        for i, a in enumerate(argv[1:], start=1):
            if a in flags and i + 1 < len(argv):
                cmd.code = argv[i + 1]
                cmd.lang = key if key not in ("nodejs", "deno", "bun") else "node"
                break
            if a == "-" or (key == "python" and a == "-"):
                cmd.stdin_script = True
                cmd.lang = key
                break
            if not a.startswith("-"):
                break
        else:
            if len(argv) == 1 and key in ("python", "node", "perl", "ruby", "php", "osascript", "lua"):
                cmd.stdin_script = True
                cmd.lang = key
        return [cmd]
    return [cmd]


def parse(command: str, _depth: int = 0) -> Parsed:
    """Read a command line. Never raises."""
    out = Parsed()
    if not isinstance(command, str) or not command.strip():
        return out
    if _depth > 8:
        out.note("commands nested too deeply to follow")
        return out
    if len(command) > 200_000:
        out.note("a command too long to read")
        return out
    inner: List[str] = []
    heredocs: List[Tuple[str, str]] = []
    try:
        toks = _tokenize(command, out, inner, heredocs)
    except _Unbalanced as e:
        out.note("an unbalanced %s" % e)
        # Still surface every word-like thing so path and program rules can look.
        words = re.findall(r"[^\s;&|<>()`'\"]+", command)
        if words:
            out.commands.append(Simple(argv=words, dynamic=True, via="unparsed"))
        return out

    # Split into simple commands.
    segments: List[List[_Tok]] = [[]]
    separators: List[str] = []
    for t in toks:
        if t.kind == "op":
            if t.text in ("(", ")"):
                separators.append(t.text)
                segments.append([])
                continue
            separators.append(t.text)
            segments.append([])
        else:
            segments[-1].append(t)
    raw_cmds: List[Simple] = []
    hd_iter = iter(heredocs)
    for seg in segments:
        if not seg:
            continue
        cmd = Simple(argv=[])
        i = 0
        saw_cmd = False
        while i < len(seg):
            t = seg[i]
            if t.kind == "redir":
                target = seg[i + 1].text if i + 1 < len(seg) and seg[i + 1].kind == "word" else ""
                cmd.redirects.append((t.text, _expand_home(target)))
                if t.text == "<<":
                    try:
                        _delim, body = next(hd_iter)
                    except StopIteration:
                        body = ""
                    cmd.code = body
                i += 2
                continue
            word = t.text
            if not saw_cmd and _ASSIGN.match(word) and t.raw[: len(word.split("=", 1)[0]) + 1].endswith("="):
                k, v = word.split("=", 1)
                cmd.assigns[k.rstrip("+")] = v
                i += 1
                continue
            if not saw_cmd and word in _KEYWORDS:
                i += 1
                continue
            if not saw_cmd:
                saw_cmd = True
                if not t.literal or t.subst or "\0" in word:
                    out.note("a program name that is only known when it runs")
                    cmd.dynamic = True
            elif not t.literal:
                cmd.dynamic = True
            cmd.argv.append(_expand_home(word))
            i += 1
        if cmd.argv or cmd.redirects or cmd.assigns:
            raw_cmds.append(cmd)

    # A here-document handed to an interpreter is that interpreter's program.
    for cmd in raw_cmds:
        if cmd.code is not None and cmd.argv:
            p = cmd.prog
            cmd.lang = "shell" if p in _SHELLS else "python" if _PYTHON.match(p) else p if p in _INLINE else None
            if cmd.lang is None and p not in ("cat", "tee"):
                cmd.lang = "text"

    # A program whose input is a pipe and which reads a script from stdin.
    for cmd in raw_cmds:
        unwrapped = _unwrap(cmd, out, _depth)
        out.stages.append(list(unwrapped))
        out.raws.append(cmd)
        for c in unwrapped:
            out.commands.append(c)
    out.separators = list(separators)
    for c in out.commands:
        if c.stdin_script:
            out.note("a script read from standard input")
        if c.prog in ("function",) or (c.argv and c.argv[0].endswith("()")):
            out.note("a shell function")

    # Substituted commands are commands too.
    for text in inner:
        sub = parse(text, _depth=_depth + 1)
        out.opaque.extend(x for x in sub.opaque if x not in out.opaque)
        for c in sub.commands:
            c.via = ("$( ) → " + c.via).strip(" →")
            out.commands.append(c)
        out.substitutions.append(text)

    # Standalone: one command, at most a leading `cd <dir> &&`, nothing else.
    seps = [s for s in separators if s != "\n" or True]
    real = [c for c in raw_cmds]
    lead_cd = None
    if len(real) == 2 and real[0].prog == "cd" and len(real[0].argv) == 2 and separators and \
            [s for s in separators if s.strip() or s == "\n"][:1] in (["&&"], [";"], ["\n"]) and \
            not real[0].redirects and not real[0].dynamic:
        lead_cd = real[0].argv[1]
        real = real[1:]
        seps = [s for s in seps][1:]
    only_newlines = all(s == "\n" for s in seps)
    if len(real) == 1 and only_newlines and not out.opaque:
        c = real[0]
        if not c.redirects and not c.assigns and not c.dynamic and c.code is None and "(" not in separators:
            out.standalone = True
            out.cd = lead_cd
    return out
