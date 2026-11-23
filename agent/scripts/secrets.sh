#!/usr/bin/env bash
# secrets.sh — the ONE way scripts read and write secrets on the owner's Macs.
#
# Store: the macOS login Keychain (the Passwords app shows the same items).
# No 1Password, no secrets in git, no secret ever on a command line.
#
# Naming convention (decided 2026-10-08):
#   service = omni.<repo>.<NAME>      account = <NAME>
#   <repo>  = agent | prism | hermes | deploy   (lowercase, [a-z0-9-])
#   <NAME>  = the environment variable it fills, e.g. TELEGRAM_BOT_TOKEN
#   e.g. omni.agent.TELEGRAM_BOT_TOKEN, omni.prism.SESSION_SECRET
#
# Usage:
#   secrets.sh --repo agent get NAME            value to stdout (for $(...) only)
#   printf %s "$v" | secrets.sh --repo agent set NAME
#                                               value from STDIN — never argv
#   secrets.sh --repo prism env-file TEMPLATE [OUT]
#                                               render a 0600 .env: every line
#                                               KEY=@keychain becomes KEY=<item
#                                               omni.<repo>.KEY>; other lines are
#                                               copied verbatim. OUT defaults to
#                                               TEMPLATE minus a trailing .tmpl
#   secrets.sh [--repo R] list                  item NAMES only, never values
#
# --repo may also come from OMNI_SECRETS_REPO.
#
# Why stdin for `set`: `security add-generic-password -w <value>` puts the
# secret in argv, readable by any process of this user via `ps` (it once leaked
# every Buzz nsec — see buzz_agents.write_secret). Given -w with no value,
# `security` reads it from stdin twice (entry + confirmation) — but keeps only 128
# characters, so longer values go through `security -i` (a command line on stdin).
#
# Exit codes: 0 ok · 1 usage / invalid input · 2 item missing · 3 keychain error
set -euo pipefail

SECURITY="${OMNI_SECURITY_BIN:-security}"   # tests point this at a fake
REPO="${OMNI_SECRETS_REPO:-}"

die() { echo "secrets.sh: $*" >&2; exit "${2:-1}"; }
usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) [ $# -ge 2 ] || usage; REPO="$2"; shift 2 ;;
    --repo=*) REPO="${1#--repo=}"; shift ;;
    -h|--help) usage ;;
    *) break ;;
  esac
done
CMD="${1:-}"; [ -n "$CMD" ] || usage; shift

valid_name() { printf '%s' "$1" | grep -Eq '^[A-Z][A-Z0-9_]{0,63}$'; }
need_repo() {
  [ -n "$REPO" ] || die "--repo is required (agent | prism | hermes | deploy)"
  printf '%s' "$REPO" | grep -Eq '^[a-z0-9][a-z0-9-]{0,31}$' || die "bad --repo: must be [a-z0-9-]"
}
service_of() { printf 'omni.%s.%s' "$REPO" "$1"; }

get_value() {  # $1 = NAME → value on stdout, exit 2 if missing
  local out rc=0
  out="$("$SECURITY" find-generic-password -s "$(service_of "$1")" -a "$1" -w 2>/dev/null)" || rc=$?
  [ "$rc" = 0 ] || return 2
  printf '%s' "$out"
}

case "$CMD" in
  get)
    need_repo; [ $# -eq 1 ] || usage
    valid_name "$1" || die "bad NAME: must be UPPER_SNAKE_CASE"
    v="$(get_value "$1")" || die "no Keychain item $(service_of "$1")" 2
    printf '%s\n' "$v"
    ;;

  set)
    need_repo; [ $# -eq 1 ] || usage
    valid_name "$1" || die "bad NAME: must be UPPER_SNAKE_CASE"
    if [ -t 0 ]; then
      printf 'Value for %s (input hidden): ' "$(service_of "$1")" >&2
      IFS= read -rs v; echo >&2
    else
      IFS= read -r v || [ -n "${v:-}" ] || true
      # anything after the first line is refused, not silently dropped
      if IFS= read -r extra; then die "value must be a single line"; fi
    fi
    [ -n "${v:-}" ] || die "empty value refused"
    case "$v" in *$'\r'*) die "value contains a carriage return" ;; esac
    # -U update in place · -T allow /usr/bin/security to read it without a prompt.
    # The value never goes on argv. `add-generic-password -w` with no value reads it from
    # stdin but keeps only the first 128 characters (2026-10-08: a 752-character vault token
    # was stored truncated, silently). So a value made of ordinary token characters is sent
    # as one `security -i` command line on stdin instead, which has no such limit.
    svc="$(service_of "$1")"
    if printf '%s' "$v" | LC_ALL=C grep -Eq '^[A-Za-z0-9._~+/=:@%,-]+$'; then
      # Replace rather than update in place: on 2026-10-08 an in-place update of an existing
      # item left macOS waiting on an approval dialog, and every later Keychain call hung
      # behind it until someone answered at the screen.
      "$SECURITY" delete-generic-password -s "$svc" -a "$1" >/dev/null 2>&1 || true
      printf 'add-generic-password -U -s %s -a %s -T /usr/bin/security -w %s\n' "$svc" "$1" "$v" \
        | "$SECURITY" -i >/dev/null 2>&1 \
        || die "Keychain write failed for $svc (locked keychain over ssh? run: security unlock-keychain)" 3
    else
      [ "${#v}" -le 100 ] || die "value is longer than 100 characters and holds characters that cannot be passed safely; refused rather than stored truncated"
      printf '%s\n%s\n' "$v" "$v" \
        | "$SECURITY" add-generic-password -U -s "$svc" -a "$1" \
            -T /usr/bin/security -w >/dev/null 2>&1 \
        || die "Keychain write failed for $svc (locked keychain over ssh? run: security unlock-keychain)" 3
    fi
    # Read it back: a stored value that differs from what was given is an error, never a success.
    [ "$(get_value "$1" 2>/dev/null || true)" = "$v" ] || die "Keychain item $svc does not read back as the value given (stored truncated or altered) — nothing should rely on it" 3
    unset v
    echo "stored $(service_of "$1")" >&2
    ;;

  env-file)
    need_repo; [ $# -ge 1 ] && [ $# -le 2 ] || usage
    tmpl="$1"; out="${2:-${1%.tmpl}}"
    [ -r "$tmpl" ] || die "cannot read template $tmpl"
    [ "$out" != "$tmpl" ] || die "OUT would overwrite the template; name the template *.tmpl or pass OUT"
    umask 077
    tmp="$(mktemp "$(dirname "$out")/.secrets-env.XXXXXX")"
    trap 'rm -f "$tmp"' EXIT
    missing=""
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        *=@keychain)
          key="${line%=@keychain}"
          valid_name "$key" || die "template line with bad key: ${key:0:40}"
          if ! val="$(get_value "$key")"; then missing="$missing $key"; continue; fi
          case "$val" in *$'\n'*|*$'\r'*) die "$key: value spans lines — refused (it could inject other keys)" ;; esac
          if printf '%s' "$val" | grep -Eq '^[A-Za-z0-9_./+=:@,-]*$'; then
            printf '%s=%s\n' "$key" "$val" >>"$tmp"
          else
            case "$val" in *"'"*) die "$key: value has a single quote and unsafe characters — refused" ;; esac
            printf "%s='%s'\n" "$key" "$val" >>"$tmp"
          fi
          ;;
        *) printf '%s\n' "$line" >>"$tmp" ;;
      esac
    done <"$tmpl"
    [ -z "$missing" ] || die "missing Keychain items:$(for k in $missing; do printf ' %s' "$(service_of "$k")"; done) — nothing written" 2
    chmod 600 "$tmp"
    mv -f "$tmp" "$out"
    trap - EXIT
    echo "wrote $out (0600)" >&2
    ;;

  list)
    [ $# -eq 0 ] || usage
    [ -z "$REPO" ] || need_repo
    prefix="omni."; [ -z "$REPO" ] || prefix="omni.$REPO."
    # dump-keychain without -d prints attributes only (never secret data).
    "$SECURITY" dump-keychain 2>/dev/null \
      | sed -n 's/^[[:space:]]*"svce"<blob>="\(omni\.[a-z0-9-]*\.[A-Z][A-Z0-9_]*\)"$/\1/p' \
      | grep -F "$prefix" | sort -u || true
    ;;

  *) usage ;;
esac
