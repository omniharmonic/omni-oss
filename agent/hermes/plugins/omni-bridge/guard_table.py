"""The guarded actions of an Omni turn — ONE table, read by policy.py, listed in
docs/omni/tool-policy.md, and tested row by row (tests/test_omni_policy.py).

An Omni turn may do anything that is not in this table. A row says what
happens instead:

  BLOCK    refused, with a message that names the legitimate way (almost always
           `omni_propose`, which puts a draft in front of the owner). Nobody can
           approve a blocked action from the app: sends and secrets do not have
           an "allow once".
  APPROVE  paused. A card appears in the Omni app with the exact command, where
           it would run and why it was flagged; Approve once runs exactly that,
           Deny or ten minutes of silence does not.
  READER   a trusted read-only command: runs without asking, with the extra
           access its row grants (the network, the Keychain, the repo's `.env`).

HOW TO ADD A ROW
  1. Add it below, in the list for its kind, with a new `id`.
  2. Add a line to `CASES` in tests/test_omni_policy.py: a command that must
     hit the row and one that must not. The test fails for a row with no case.
  3. Add it to the table in docs/omni/tool-policy.md.
Rows are matched in the order BLOCK → READER → APPROVE; inside a kind, first
match wins. Matching is on the program a command line would run (after `sh
-c`, `env`, `sudo`, `xargs`, `find -exec`, `$( )` and friends are followed),
its script, its arguments, and — for inline code — its text. Matching decides
only the routing: whatever a command is, it still runs inside the OS sandbox.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Pattern, Sequence, Tuple

BLOCK, APPROVE, READER = "block", "approve", "reader"

_NOTIFY = "That notifies another person: draft it with omni_propose (kind message) so the owner approves it first."
PROPOSE = ("Draft it with omni_propose instead: The owner sees the exact draft in the Omni app and "
           "nothing leaves until he approves it there.")


@dataclass(frozen=True)
class Rule:
    id: str
    action: str
    #: Short name, shown on the approval card and in the docs table.
    title: str
    #: Why — to the model on a block, on the card for an approval.
    why: str
    #: Program basenames (after wrappers are followed). Empty = any.
    prog: Tuple[str, ...] = ()
    #: Script basenames run by an interpreter (`python3 scripts/x.py` → `x.py`).
    script: Tuple[str, ...] = ()
    #: Regex the arguments (joined by one space, after the program / script) must match.
    args: Optional[str] = None
    #: Regex the arguments must NOT match.
    unless: Optional[str] = None
    #: Regex on the whole command text / inline code (used when there is no program to name).
    text: Optional[str] = None
    #: omni_propose kind that is the legitimate path (BLOCK rows).
    propose: Optional[str] = None
    # ── READER grants ──
    network: bool = False
    keychain: bool = False
    #: Secret places this reader may read: "repo-env", "gh", "gog", "ssh", "pm2".
    secrets: Tuple[str, ...] = ()
    #: The script must be THIS repo's (scripts/<name>), run from the repo.
    repo_script: bool = False
    #: A signed-out Chrome may start: launchservicesd, Chrome's crash database, a profile root
    #: in the workspace, and `--no-sandbox` (sandbox.profile(browser=True), policy._browser_line).
    browser: bool = False


def _r(p: Optional[str]) -> Optional[Pattern]:
    return re.compile(p, re.IGNORECASE | re.DOTALL) if p else None


# ═════════════════════════════════════════════════════════════════════════════
# 1. OUTWARD SENDS — always an approval card for the DRAFT, never a direct run.
#    Sources: CLAUDE.md "Accounts and email", "Approval Gate", "Wallet",
#    "Privacy Boundaries"; docs/omni/integration-contract.md § 6.
# ═════════════════════════════════════════════════════════════════════════════
SENDS: Tuple[Rule, ...] = (
    Rule("send.proton", BLOCK, "Email as the owner (proton_send.py)",
         "Sending mail as the owner is never run directly. " + PROPOSE, script=("proton_send.py",), propose="email"),
    Rule("send.smtp-tools", BLOCK, "Mail from the shell (sendmail, mail, msmtp, mutt, swaks)",
         "Mail is not sent from the shell. " + PROPOSE,
         prog=("sendmail", "mail", "mailx", "msmtp", "mutt", "neomutt", "swaks", "ssmtp", "exim", "postfix"), propose="email"),
    Rule("send.smtp-curl", BLOCK, "Mail through curl (smtp://, --mail-rcpt)",
         "Mail is not sent from the shell. " + PROPOSE, prog=("curl",), args=r"smtps?://|--mail-(rcpt|from)", propose="email"),
    Rule("send.smtp-code", BLOCK, "Mail from inline code (smtplib and friends)",
         "Mail is not sent from code. " + PROPOSE,
         text=r"\bsmtplib\b|\bSMTP(_SSL)?\s*\(|nodemailer|Net::SMTP|\bimport\s+yagmail|sendgrid|mailgun|api\.resend\.com|postmarkapp", propose="email"),
    # gog's aliases are matched too: `mail`/`email` = gmail, `cal` = calendar, and a
    # top-level `gog send` is Gmail's send (review round 1, B1).
    # Work unblock 3: the args regexes of the gog SEND rows only PRE-FILTER (they may over-match free
    # text); policy._GOG_NOTIFY_CHECKS decides each on gog's command words (`_gog_head`), so a sheet
    # cell or a doc body that says "email … reply" is not a send. An unreadable head fails closed.
    Rule("send.gog-gmail", BLOCK, "Gmail send / reply / forward / autoreply / draft-send (gog, any alias)",
         "Mail is never sent with gog. " + PROPOSE, prog=("gog", "gws"),
         args=r"\b(gmail|mail|email)\b.*(\b|\+)(send|reply|forward|fwd|autoreply|drafts?|messages?)\b|"
              r"^\s*(?:-{1,2}[\w-]+(?:=\S+|\s+[^-\s]\S*)?\s+)*(send|reply|forward|fwd)\b", propose="email"),
    # Fix round 1 (I2): Gmail settings that send mail to other people or hand the mailbox over —
    # forwarding, auto-forward, the vacation responder (same as autoreply), send-as, delegates, and a
    # filter that forwards. policy._gog_gmail_settings decides on command words; listing / get runs.
    Rule("send.gmail-settings", BLOCK, "Gmail forwarding / auto-forward / vacation responder / send-as / delegates / a forwarding filter",
         "These send mail to other people or hand the mailbox to someone else. " + PROPOSE,
         prog=("gog", "gws"), args=r"\b(gmail|mail|email)\b", propose="email"),
    Rule("send.calendar-rsvp", BLOCK, "Calendar RSVP (gog calendar|cal respond / rsvp / reply)",
         "RSVP notifies the organizer. Draft the exact eventId and response with omni_propose (kind calendar-rsvp); the owner approves that exact response.",
         prog=("gog", "gws"), args=r"\b(calendar|cal)\b.*\b(respond|rsvp|reply)\b", propose="calendar-rsvp"),
    Rule("send.calendar-invite", BLOCK, "Calendar event that notifies other people (gog calendar|cal create / add / update / edit / delete … --attendees / --add-attendee / --send-updates)",
         "An event with other attendees sends them an invitation. Draft it with omni_propose (kind calendar-invite).",
         prog=("gog", "gws"), args=r"\b(calendar|cal)\b.*(--attendees?\b|--send-updates?\b|--add-attendees?\b|--notify\b)",
         propose="calendar-invite"),
    # Work unblock 13: blocks of the owner's own time that decline other people's invitations.
    Rule("send.calendar-decline", BLOCK, "Focus time / out of office that auto-declines, or propose-time (gog calendar focus-time / ooo / propose-time …)",
         "Auto-declining sends each organizer a decline, and propose-time exists to tell the organizer. To block the time "
         "only, add --auto-decline none (focus-time / out-of-office) or --focus-auto-decline none / --ooo-auto-decline none "
         "(create / update). If the declines should go out, draft it with omni_propose (kind message) so the owner decides.",
         prog=("gog", "gws"), args=r"\b(calendar|cal)\b", propose="message"),
    Rule("send.gog-chat", BLOCK, "Google Chat message or space change (gog chat … anything but list / get / search)",
         "Google Chat messages reach other people. Draft it with omni_propose (kind message).",
         prog=("gog", "gws"), args=r"\bchat\b", propose="message"),
    # Google actions that notify another person (round 3). The args regex only pre-filters;
    # policy._GOG_NOTIFY_CHECKS confirms each on gog's command WORDS, not on free text.
    Rule("send.google-share", BLOCK, "Share a Google file with someone (gog drive|docs|… share, permissions create / add / update)",
         "Sharing a file emails that person a notification. %s Removing access (unshare, permissions delete) runs as usual." % _NOTIFY,
         prog=("gog", "gws"), args=r"\b(share|permissions?|perms)\b", propose="message"),
    Rule("send.google-groups", BLOCK, "Add or invite someone to a Google Group (gog groups … add / insert / invite)",
         "Adding or inviting a member emails that person. %s" % _NOTIFY, prog=("gog", "gws"), args=r"\bgroups\b", propose="message"),
    Rule("send.google-classroom", BLOCK, "Any Google Classroom change (gog classroom | class … anything but list / get / search)",
         "Nearly every Classroom change (posts, coursework, materials, invitations, roster) notifies students, teachers or guardians. %s" % _NOTIFY,
         prog=("gog", "gws"), args=r"\bclass(room)?\b", propose="message"),
    Rule("send.google-comment-mention", BLOCK, "A Docs / Drive comment that mentions or assigns someone (@name, +email, --assign, --mention)",
         "Mentioning or assigning someone in a comment emails them. %s A comment with no mention runs as usual." % _NOTIFY,
         prog=("gog", "gws"), args=r"\bcomments?\b", propose="message"),
    Rule("send.keep-share", BLOCK, "Share a Keep note (gog keep … share, collaborators add)",
         "Sharing a note, or adding a collaborator, notifies that person. %s" % _NOTIFY, prog=("gog", "gws"), args=r"\bkeep\b", propose="message"),
    Rule("send.hermes-cli", BLOCK, "Hermes' own send command (hermes send → Telegram / Discord / Slack / Signal / SMS)",
         "hermes send delivers with the gateway's tokens to anyone. Draft it with omni_propose (kind message).",
         prog=("hermes", "hermes-agent"), args=r"^\s*(?:-{1,2}[\w-]+(?:=\S+|\s+[^-\s]\S*)?\s+)*send\b", propose="message"),
    Rule("send.hermes-meet", BLOCK, "Join, speak in or sign in to a meeting from the shell (hermes meet / omni-meet)",
         "Meetings go through omni_meet_attend / omni_meet_allow_speech, each approved by the owner in the Omni app: "
         "omni_propose kind action with grants meet.attend (and meet.speak).",
         prog=("hermes", "hermes-agent"), propose="action",
         args=r"^\s*(?:-{1,2}[\w-]+(?:=\S+|\s+[^-\s]\S*)?\s+)*(?:omni-)?meet\s+(?:-{1,2}[\w-]+\s+)*(?!(?:status|transcript|setup|help)\b)[a-z]"),
    Rule("send.applescript", BLOCK, "A message sent by scripting an app (osascript: Mail, Messages, Outlook, Slack, Telegram, WhatsApp …)",
         "Scripting a signed-in app sends as the owner with no approval. Draft it with omni_propose.",
         prog=("osascript",),
         args=r"\bapplication\s*\(?\s*[\"']?(Mail|Messages|Microsoft Outlook|Outlook|Slack|Telegram|WhatsApp|Signal|Discord|Spark|Airmail)\b|\bsend\b",
         text=r"\bapplication\s*\(?\s*[\"']?(Mail|Messages|Microsoft Outlook|Outlook|Slack|Telegram|WhatsApp|Signal|Discord|Spark|Airmail)\b",
         propose="message"),
    Rule("send.shortcuts", BLOCK, "Run a Shortcut (shortcuts run) — it can send messages or mail as the owner",
         "A Shortcut can send as the owner with no approval. Draft it with omni_propose.", prog=("shortcuts",), args=r"^\s*run\b",
         propose="message"),
    Rule("send.google-api", BLOCK, "Gmail send/reply, a calendar invite or a Drive share through Hermes' google-workspace skill (google_api.py)",
         "google_api.py sends as the signed-in Google account with no approval. Draft it with omni_propose (kind email or calendar-invite).",
         script=("google_api.py",),
         # Review R4 I-b: argparse accepts option ABBREVIATIONS (`--attendee`, `--att`), so the
         # calendar write subcommands are refused outright (with or without attendees), as are
         # any send/reply/forward and every drafts … send. Subcommand prefixes are matched too.
         args=(r"^\s*(\S+\s+)*?(gmail|mail)\s+(sen\w*|repl\w*|forw\w*|draf\w*\s+(\S+\s+)*?sen\w*|send-draft|draft-send)\b"
               r"|^\s*(\S+\s+)*?(calendar|cal)\s+(cr\w*|up\w*|ins\w*|pa\w*|imp\w*|quick-?add|resp\w*)\b"
               r"|^\s*(\S+\s+)*?drive\s+sh\w*\b"),
         propose="email"),
    Rule("send.google-api-text", BLOCK, "google_api.py run as a module or imported (python -m google_api, import google_api …)",
         "google_api.py sends as the signed-in Google account with no approval. Draft it with omni_propose (kind email or calendar-invite).",
         text=(r"\bgoogle_api\b[^\n]*?\b(gmail|mail)\s+(sen|repl|forw|draf)\w*|\bgoogle_api\b[^\n]*?\b(calendar|cal)\s+(cr|up|ins|pa|imp|resp)\w*"
               r"|\bgoogle_api\b[^\n]*?\bdrive\s+sh\w*|\bgoogle_api\b[^\n]*?\b(gmail_send|gmail_reply|gmail_forward|calendar_create|calendar_update|drive_share)\b"
               # a subcommand group named through a shell variable: `google_api.py $S send`
               r"|\bgoogle_api(\.py)?\b[^\n]*?\$\{?\w+\}?\s+(sen|repl|forw|draf|cr|up|ins|pa|imp|resp|sh)\w*"),
         propose="email"),
    Rule("send.tweet", BLOCK, "Post to X / Twitter (twitter_post.py)",
         "Posts are never published directly. Draft it with omni_propose (kind tweet).", script=("twitter_post.py", "omni_tweet_send.py"), propose="tweet"),
    Rule("send.tweet-api", BLOCK, "Post to X / Twitter through its API",
         "Posts are never published directly. Draft it with omni_propose (kind tweet).",
         text=r"api\.(twitter|x)\.com/[12](\.1)?/(tweets|statuses/update)|\btweepy\b|\bcreate_tweet\s*\(", propose="tweet"),
    Rule("send.wallet", BLOCK, "Wallet / Safe / on-chain write (wallet.py propose-transfer, create_safe.py, cast send)",
         "Nothing that signs or moves value is run directly. Draft it with omni_propose (kind wallet-proposal).",
         script=("wallet.py",), args=r"^(?!\s*(balance|pending|history)\b)", propose="wallet-proposal"),
    Rule("send.wallet-tools", BLOCK, "On-chain write from the shell (cast send, safe-cli, create_safe.py)",
         "Nothing that signs or moves value is run directly. Draft it with omni_propose (kind wallet-proposal).",
         prog=("cast", "safe-cli", "safe", "seth", "forge"), args=r"\b(send|publish|tx\b|transaction|create|script\b.*--broadcast|mktx|wallet\s+(sign|import|new))",
         propose="wallet-proposal"),
    Rule("send.wallet-safe-script", BLOCK, "Create or change a Safe (create_safe.py)",
         "Nothing that signs or moves value is run directly. Draft it with omni_propose (kind wallet-proposal).",
         script=("create_safe.py",), propose="wallet-proposal"),
    Rule("send.wallet-code", BLOCK, "Signing or sending a transaction from inline code",
         "Nothing that signs or moves value is run from code. Draft it with omni_propose (kind wallet-proposal).",
         text=r"send_raw_transaction|sign_transaction|eth_sendRawTransaction|\.sendTransaction\s*\(|safe-transaction-service|AGENT_WALLET_PRIVATE_KEY",
         propose="wallet-proposal"),
    Rule("send.chat-api", BLOCK, "Message to other people through a chat API (Telegram, Matrix, Slack, WhatsApp, Discord, Signal)",
         "Messages to other people are never sent directly. Draft it with omni_propose (kind message).",
         text=(r"api\.telegram\.org/bot[^\s/]*/(send|forward|copy|edit)|/_matrix/client/[^\s]*/(send|rooms/[^\s]*/send)|"
               r"slack\.com/api/(chat\.|files\.upload)|hooks\.slack\.com/|graph\.facebook\.com/[^\s]*/messages|"
               r"discord(app)?\.com/api/(webhooks|v\d+/channels/[^\s]*/messages)|signal-cli\b.*\bsend\b"),
         propose="message"),
    Rule("send.matrix-reader", BLOCK, "Direct Matrix CLI send",
         "Draft the exact recipient and text with omni_propose (kind message); the owner approves that exact draft.",
         script=("matrix_reader.py",), args=r"^\s*(send|send-to)\b", propose="message"),
    Rule("send.chat-scripts", BLOCK, "Message to other people through a repo script (Matrix, dispatch)",
         "Messages to other people are never sent directly. Draft it with omni_propose (kind message).",
         script=("matrix_send.py", "send_matrix.py", "dispatch_task.py", "meeting_followup.py"), propose="message"),
)

# ═════════════════════════════════════════════════════════════════════════════
# 2. SECRETS — never read, never printed. (Paths are in paths.py; these rows are
#    the commands that DUMP secrets without naming a file.)
# ═════════════════════════════════════════════════════════════════════════════
SECRETS: Tuple[Rule, ...] = (
    Rule("secret.keychain", BLOCK, "Read the Keychain (security find-…, dump-keychain, export)",
         "The Keychain holds the owner's credentials; it is not readable from an Omni conversation.",
         prog=("security",)),
    Rule("secret.process-env", BLOCK, "Other processes' environment (ps e / ps -E, /proc/…/environ)",
         "Other processes' environments hold tokens. Use `pgrep -l <name>` to see what is running.",
         text=r"/proc/[^\s/]+/environ|\bps\s+(-\w+\s+)*-?\w*[eE]\w*\b"),
    Rule("secret.launchctl-env", BLOCK, "launchd environments (launchctl print / getenv / export / procinfo)",
         "launchd's view of a service includes its environment. Ask the owner for what you need to know.",
         prog=("launchctl",), args=r"^\s*(print|print-cache|getenv|export|procinfo|dumpstate)\b"),
    Rule("secret.pm2-env", BLOCK, "pm2 process environments (pm2 env / jlist / prettylist / describe / show / dump)",
         "pm2's process records include each service's environment (its tokens).",
         prog=("pm2",), args=r"^\s*(env|jlist|prettylist|describe|show|desc|info|dump|save|report)\b"),
    Rule("secret.hermes-config", BLOCK, "Hermes' own settings and credentials (hermes config / auth / secrets)",
         "Hermes' settings hold its credentials and this conversation's own guard rules.",
         prog=("hermes",), args=r"^\s*(config|auth|secrets?|login|logout|setup|doctor|env)\b"),
    # Work unblock 2: agent-browser may run Chrome, but only SIGNED OUT. Attaching to a running
    # Chrome, the owner's own profiles, saved sign-in state, the auth vault and cloud providers
    # are his sessions (raw credentials). policy._browser_signed_in confirms each on the argv.
    Rule("secret.browser-session", BLOCK, "A browser with the owner's sign-ins (agent-browser --auto-connect / --cdp / connect / --profile <his> / --restore / auth …)",
         "The agent's browser is signed out. Attaching to a running Chrome, using a Chrome profile outside the workspace, "
         "restoring saved sign-in state or the auth vault would act with the owner's own sessions. Use a fresh session, or a "
         "profile / --state file inside the workspace (e.g. --profile ~/omni-workspace/.browser-profiles/NAME).", prog=("agent-browser",)),
    Rule("secret.token-mint", BLOCK, "Mint or print a credential (parachute auth, gh auth token, gcloud auth print-…, op read)",
         "Minting or printing a credential is not something an Omni conversation does. For the vault, use the "
         "mcp__parachute__* tools (query_notes, create_note, update_note); Hermes already authenticates them.",
         text=r"\bparachute\s+auth\b|\bgh\s+auth\s+(token|status\b[^\n]*?(\s-t\b|\s-[a-z]*t\b|--show-token)|login|refresh|setup-git)|\bgcloud\s+auth\s+print|\bop\s+(read|item\s+get|signin)\b|\bwallet\.py\s+keychain-import\b|\bgog\s+auth\s+(add|token|print|export)"),
)

# ═════════════════════════════════════════════════════════════════════════════
# 3. THE GUARD ITSELF — cannot be switched off from inside. BLOCK, not APPROVE:
#    The owner changes these from his own terminal, never through the agent.
# ═════════════════════════════════════════════════════════════════════════════
SELF: Tuple[Rule, ...] = (
    Rule("self.proton-owner-auth", BLOCK, "Owner-only local Keychain authorization",
         "This local macOS authentication flow is never run by an agent or command approval. The owner runs the named authorize-keychain command himself in the agent host GUI Terminal.",
         script=("proton_mail.py",), args=r"\bauthorize-keychain\b"),
    Rule("self.os-schedule", BLOCK, "Schedule work outside Hermes (crontab, at, batch)",
         "A job scheduled with the operating system would run later with none of these rules. "
         "Use the cronjob tool instead: The owner approves it, and the job runs under the Omni policy.",
         prog=("crontab", "at", "batch", "atrm", "atq")),
    Rule("self.launchd-env", BLOCK, "Change the environment services start with (launchctl setenv / unsetenv / config)",
         "This conversation cannot change its own guard rules, or the environment the services start with.",
         prog=("launchctl",), args=r"^\s*(setenv|unsetenv|config|limit)\b"),
    Rule("self.sandbox", BLOCK, "Leave or rewrite the sandbox (sandbox-exec, csrutil, spctl, tccutil, dscl)",
         "The sandbox is part of the guard.", prog=("sandbox-exec", "csrutil", "spctl", "tccutil", "dscl", "chflags")),
    Rule("self.setuid", BLOCK, "Programs macOS will not run inside the sandbox (sudo, su, doas, ps)",
         "macOS refuses to start this program inside the sandbox every Omni command runs in. For what is running use "
         "`pgrep -l <name>`; for what listens on which port (with the process name and pid) `netstat -anv -p tcp | grep LISTEN`; "
         "for anything that needs administrator rights, tell the owner the exact command to run himself.",
         prog=("sudo", "su", "doas", "ps", "top", "login", "passwd", "newgrp", "at", "atq", "atrm", "batch", "crontab")),
    # self.files (policy.py): any write aimed at the guard's own files — this plugin, Hermes'
    # home, launchd plists, shell start-up files. Paths: paths.guard_paths().
    # self.skill-repo (policy.py, EVERY turn): a skill_manage call whose target resolves outside
    # ~/.hermes/skills — the agent repo, a skills.external_dirs entry, or a symlink into either.
    # Paths: paths.skill_write_protected().
)

# ═════════════════════════════════════════════════════════════════════════════
# 4. TRUSTED READERS — run without asking, with their own access.
#    Each must be the WHOLE command (optionally after `cd <repo> &&`); a `$( )`
#    inside may only run `date` / `echo` / `printf`. Or its output is only filtered /
#    saved (policy._grant_head: file-less filters, one trailing `>` into the workspace).
# ═════════════════════════════════════════════════════════════════════════════
READERS: Tuple[Rule, ...] = (
    Rule("read.matrix", READER, "Read Matrix rooms and messages",
         "Bounded Matrix reads; sends use omni_propose instead.", script=("matrix_reader.py",),
         args=r"^\s*(rooms|recent|read|search|find-room|--help|-h)(\s|$)", network=True, secrets=("repo-env",), repo_script=True),
    Rule("read.gog-calendar", READER, "Read the calendar (gog calendar events / list / get / freebusy / calendars)",
         "Calendar reads.", prog=("gog",), args=r"^\s*calendar\s+(events|list|get|show|freebusy|calendars|colors|search|agenda)\b",
         unless=r"--attendees?\b|--send-updates?\b", network=True, keychain=True, secrets=("gog",)),
    Rule("read.gog-drive", READER, "Read Drive / Docs / Sheets (gog drive ls / search / get / download, docs cat, sheets get)",
         "Drive reads.", prog=("gog",),
         args=r"^\s*(drive\s+(ls|list|search|get|info|download|export|permissions\s+list)|docs\s+(cat|get|export|info)|sheets\s+(get|metadata|values\s+get)|contacts\s+(list|search|get)|tasks\s+(list|lists|get)|people\s+\w*(get|search|list))\b",
         network=True, keychain=True, secrets=("gog",)),
    Rule("read.gog-gmail", READER, "Read the agent's Gmail (gog gmail search / get / thread / labels)",
         "Mail reads.", prog=("gog",), args=r"^\s*gmail\s+(search|get|thread|threads|labels|list|messages\s+(list|get|search)|attachment)\b",
         network=True, keychain=True, secrets=("gog",)),
    Rule("read.gog-auth-list", READER, "Which Google accounts exist (gog auth list)",
         "Account list.", prog=("gog",), args=r"^\s*auth\s+list\b", keychain=True, secrets=("gog",)),
    # Any other gog / gws READ (policy._gog_read_only: forms, slides, people, keep … reads), so a
    # newly granted OAuth scope needs no guard edit. Its non-reads are sys.google-write.
    Rule("read.gog-any", READER, "Any other Google read (gog forms get / raw, slides get, people me, keep list …)",
         "Google reads.", prog=("gog", "gws"), unless=r"--attendees?\b|--send-updates?\b", network=True, keychain=True, secrets=("gog",)),
    Rule("read.omni-cli", READER, "The agent's own CLI, read side (omni_cli.py tasks / calendar / open-loops)",
         "Tasks, calendar, open loops.", script=("omni_cli.py",),
         args=r"^\s*(tasks(\s+list\b.*)?(\s+--json)?\s*$|calendar\b|open-loops\b)",
         unless=r"--commit\b", network=True, keychain=True, secrets=("repo-env", "gog"), repo_script=True),
    Rule("read.omni-nudge-sweep", READER, "Evidence-based owner nudge proposals / calendar busy context",
         "Reads local vault/mail/calendar; commits only owner gateway proposals, never correspondence.",
         script=("omni_cli.py",), args=r"^\s*sweep-owed(?:\s+--(?:commit|context-only|weekly-audit)){0,3}\s*$",
         network=True, keychain=True, secrets=("repo-env", "gog", "proton-state"), repo_script=True),
    Rule("read.omni-briefing", READER, "The agent's briefings on demand (omni_cli.py morning / sense / evening / alerts, without --commit)",
         "Relays what the routines wrote; never marks anything relayed.", script=("omni_cli.py",),
         args=r"^\s*(morning|sense|evening|alerts)\b", unless=r"--commit\b",
         network=True, keychain=True, secrets=("repo-env", "gog", "claude"), repo_script=True),
    Rule("read.omni-tasks-write", READER, "The owner's own task list (omni_cli.py task-add, tasks done|drop)",
         "His own tasks, in his own vault.", script=("omni_cli.py",), args=r"^\s*(task-add\b|tasks\s+(done|drop)\b)",
         network=True, keychain=True, secrets=("repo-env",), repo_script=True),
    Rule("read.proton-search", READER, "Read the owner's inbox (proton_mail.py search / health / attachment / message)",
         "Inbox reads through the local Bridge.", script=("proton_mail.py",), args=r"^\s*(search|health|attachment|message)\b",
         network=True, keychain=True, secrets=("repo-env", "proton-state"), repo_script=True),
    Rule("read.wallet", READER, "Wallet balances and history (wallet.py balance / pending / history)",
         "Read-only chain queries.", script=("wallet.py",), args=r"^\s*(balance|pending|history)\b",
         network=True, secrets=("repo-env",), repo_script=True),
    Rule("read.slots", READER, "Find free time (find_slots.py, find_mutual_slots.py)",
         "Availability.", script=("find_slots.py", "find_mutual_slots.py"), network=True, keychain=True, secrets=("repo-env", "gog"), repo_script=True),
    Rule("read.telegram-self", READER, "A note to the owner's own Telegram chat (send_telegram.py)",
         "Goes only to the owner's own chat.", script=("send_telegram.py",), unless=r"--chat(-id)?\b|--to\b",
         network=True, secrets=("repo-env",), repo_script=True),
    Rule("read.gh", READER, "Read GitHub (gh pr|issue|repo|run|release list/view/status/diff/checks, gh search, gh api GET)",
         "GitHub reads.", prog=("gh",),
         args=r"^\s*((pr|issue|repo|run|release|workflow|gist|label|project)\s+(list|view|status|diff|checks|watch)\b|search\b|status\b|api\s+(?!.*(-X|--method)\s*(POST|PUT|PATCH|DELETE))(?!.*(\s-f\b|\s-F\b|--field|--raw-field|--input)))",
         network=True, keychain=True, secrets=("gh",)),
    Rule("read.git-fetch", READER, "Fetch from a repository's own remote (git fetch / pull / ls-remote)",
         "Reads from the remote the repository already has.", prog=("git",), args=r"^\s*(-C\s+\S+\s+)?(fetch|pull|ls-remote|remote\s+(-v|show|get-url)\b)",
         unless=r"\b(https?|ssh|git)://|\S+@\S+:", network=True, keychain=True, secrets=("ssh", "gh")),
)

# ═════════════════════════════════════════════════════════════════════════════
# 5. NEEDS THE OWNER'S APPROVAL — an approval card with the exact command.
# ═════════════════════════════════════════════════════════════════════════════
NETWORK_PROGS = ("curl", "wget", "http", "https", "xh", "aria2c", "nc", "ncat", "netcat", "socat", "telnet", "ftp", "sftp", "tftp",
                 "ssh", "scp", "rsync", "mosh", "nmap", "ping", "ping6", "traceroute", "dig", "nslookup", "host", "whois",
                 "openssl", "websocat", "lftp", "rclone", "aws", "gcloud", "gsutil", "az", "kubectl", "docker", "podman",
                 "open", "tailscale", "ngrok", "cloudflared")
INSTALL_PROGS = ("pip", "pip3", "pipx", "uv", "npm", "npx", "pnpm", "yarn", "bun", "brew", "port", "gem", "cargo", "go",
                 "conda", "mamba", "poetry", "apt", "apt-get", "mas", "softwareupdate", "installer")
#: Programs that start Chrome / Chromium (`Google Chrome` is the app binary's name).
BROWSER_PROGS = ("agent-browser", "playwright", "chromium", "chromium-browser", "chrome", "google-chrome", "Google Chrome",
                 "Google Chrome for Testing", "Chromium", "chrome-headless-shell", "headless_shell", "puppeteer")
AGENT_PROGS = ("hermes", "claude", "codex", "gemini", "aider", "opencode", "goose", "buzz-acp", "hermes-acp", "claude-agent-acp")

APPROVALS: Tuple[Rule, ...] = (
    # ── b. a signed-out browser started from the shell (work unblock 2) ──
    Rule("sys.browser", APPROVE, "Start a signed-out Chrome from the shell (agent-browser, Chrome, Chromium, Playwright)",
         "Starts a browser that reaches the web. It is signed out: a fresh profile, or one kept in the workspace.",
         prog=BROWSER_PROGS, browser=True),
    # ── c. network egress ──
    Rule("net.program", APPROVE, "Reach the network from the shell",
         "This command talks to another machine. Anything it can read could leave with it.", prog=NETWORK_PROGS),
    Rule("net.git-remote", APPROVE, "git push / clone / remote add / submodule (talks to a remote, or changes where it points)",
         "Sends commits to, or fetches code from, a remote.", prog=("git",),
         args=r"^\s*(-C\s+\S+\s+)?(push|clone|remote\s+(add|set-url|rename|remove|rm)|submodule\s+(add|update)|send-email|archive\s+--remote|lfs\s+push|fetch\s+\S*(://|@))"),
    Rule("net.install", APPROVE, "Install or download software (pip, npm, brew, uv, cargo, go install …)",
         "Installs run other people's code and reach the network.", prog=INSTALL_PROGS,
         args=r"\b(install|add|i|ci|update|upgrade|sync|get|run|exec|dlx|x|tap|fetch|download|publish|login|create|init)\b|^\s*$"),
    Rule("net.python-server", APPROVE, "Start a server or a module that talks to the network (python -m http.server / pip / …)",
         "Opens a port or reaches the network.", text=r"\bpython[0-9.]*\s+(-\w+\s+)*-m\s+(http\.server|pip|ensurepip|smtpd|ftplib|webbrowser|uvicorn|gunicorn|flask|aiohttp)"),
    Rule("net.code", APPROVE, "Inline code that opens a network connection",
         "This code talks to another machine. Anything it can read could leave with it.",
         text=(r"\b(import|from)\s+(socket|urllib|requests|httpx|aiohttp|http\.client|ftplib|telnetlib|websocket|websockets|paramiko|"
               r"smbprotocol|boto3|xmlrpc|poplib|imaplib|nntplib)\b|\burllib\.request\b|\burlopen\s*\(|\brequests\.(get|post|put|patch|delete|head|request)\b|"
               r"\bfetch\s*\(\s*['\"]https?://(?!127\.0\.0\.1|localhost|\[::1\])|require\s*\(\s*['\"](node:)?(https?|net|dgram|tls|dns)['\"]|"
               r"\bXMLHttpRequest\b|Net::HTTP|LWP::|IO::Socket|TCPSocket|/dev/(tcp|udp)/|\bdo\s+shell\s+script\b")),
    # ── d. destructive / system-level ──
    Rule("sys.services", APPROVE, "Start, stop or restart a service (launchctl, pm2, brew services, systemctl, kickstart)",
         "Changes what is running on the agent host.", prog=("launchctl", "pm2", "systemctl", "service", "supervisorctl", "pkill", "killall", "kill", "shutdown", "reboot", "halt")),
    Rule("sys.brew-services", APPROVE, "Homebrew services", "Changes what is running on the agent host.", prog=("brew",), args=r"\bservices?\b"),
    Rule("sys.agents", APPROVE, "Start another agent from the shell (hermes, claude, codex …)",
         "Another agent process would act with its own rules, not this conversation's.", prog=AGENT_PROGS),
    Rule("sys.automation", APPROVE, "Drive the Mac's apps (osascript, shortcuts, automator, open -a, defaults write)",
         "Automation can act as the owner in any app that is signed in.",
         prog=("osascript", "shortcuts", "automator", "defaults", "pmset", "networksetup", "scutil", "systemsetup", "diskutil", "hdiutil", "say", "pbpaste", "pbcopy", "tmux", "screen")),
    Rule("sys.rm-recursive", APPROVE, "Delete recursively outside the workspace (rm -r, find -delete)",
         "Deletes more than one named file.", prog=("rm", "rmdir", "srm", "unlink", "shred", "trash"),
         args=r"(^|\s)-[A-Za-z]*[rRf][A-Za-z]*(\s|$)|--recursive|--force|--no-preserve-root"),
    Rule("sys.find-delete", APPROVE, "Delete what a search finds (find … -delete)", "Deletes every match.", prog=("find", "gfind", "fd"),
         args=r"(^|\s)-delete\b"),
    Rule("sys.git-destructive", APPROVE, "Throw away work in a repository (git reset --hard, clean -f, checkout -- ., push --force, branch -D, filter-branch)",
         "Uncommitted or unpushed work can be lost.", prog=("git",),
         args=r"\breset\s+(.*\s)?--hard\b|\bclean\s+(.*\s)?-[A-Za-z]*[fdxX]|\bcheckout\s+(--\s+)?\.(\s|$)|\brestore\s+(--\S+\s+)*\.(\s|$)|\bbranch\s+(.*\s)?-D\b|"
              r"\bfilter-(branch|repo)\b|\brebase\b|\bstash\s+(drop|clear)\b|\bupdate-ref\s+-d\b|\breflog\s+expire\b|\bgc\s+.*--prune"),
    # Talking to people on GitHub. OUTWARD: stays an approval card under internal autonomy.
    Rule("sys.gh-notify", APPROVE, "Comment or review on GitHub (gh issue|pr comment, gh pr review, gh api …/comments|/reviews)",
         "Other people read it and are notified.", prog=("gh",),
         args=r"^\s*((issue|pr)\s+comment\b|pr\s+review\b)|\bapi\b(?=.*/(comments|reviews|replies)\b)(?=.*((-X|--method)\s*(POST|PUT|PATCH)|\s-f\b|\s-F\b|--field|--raw-field|--input))"),
    Rule("sys.gh-write", APPROVE, "Change GitHub (gh pr / issue / release / repo create, merge, close, edit; gh api with a body)",
         "Changes the owner's GitHub.", prog=("gh",),
         args=r"\b(create|comment|merge|close|reopen|edit|review|delete|ready|lock|transfer|fork|sync|rename|archive|upload|dispatch|rerun|cancel|enable|disable|set|pin)\b|"
              r"\bapi\b.*((-X|--method)\s*(POST|PUT|PATCH|DELETE)|\s-f\b|\s-F\b|--field|--raw-field|--input)"),
    Rule("sys.calendar-write", APPROVE, "Change the calendar without notifying anyone (gog calendar create / update / delete)",
         "Changes the owner's calendar.", prog=("gog", "gws"), args=r"\b(calendar|cal)\b"),
    # Work unblock 12: Gmail housekeeping (policy._gog_gmail_housekeeping decides on command words).
    Rule("sys.gmail-housekeeping", APPROVE, "Tidy a Gmail mailbox (gog gmail archive / trash / untrash / mark-read / unread, messages|thread|batch modify, labels list|create|modify)",
         "Changes labels and read state in a Gmail mailbox; reaches nobody. Gmail's settings, filters, forwarding, send-as, "
         "delegates, vacation replies and every send stay refused.", prog=("gog",), args=r"\b(gmail|mail|email)\b"),
    # Holds have no attendees and no flag can add one (manage_holds.py has none), so it sends nothing. It needs only gog's
    # token store, not the repo's .env. Omni runs it in any turn under internal autonomy; the invite stays send.calendar-invite.
    Rule("sys.calendar-holds", APPROVE, "Create or clear calendar HOLDs (manage_holds.py)", "Changes the owner's calendar.",
         script=("manage_holds.py",), network=True, keychain=True, secrets=("gog",)),
    # Also every other WHOLE-command gog / gws that is not a read and not Gmail / Chat / gog's own
    # credentials or settings (policy._gog_any_write) — forms publish, add-question, slides create …
    Rule("sys.google-write", APPROVE, "Change Google data (any gog command that is not a read)",
         "Changes the owner's Google data (Drive, Docs, Sheets, Forms, Slides, Contacts, Tasks …), or who can see it.", prog=("gog", "gws"),
         args=r"\b(drive|docs|sheets|slides|contacts|tasks|forms|people)\b.*\b(create|update|delete|trash|share|unshare|upload|move|copy|rename|append|clear|write|insert|batch\w*|permissions\s+(create|add|delete|update))\b"),
    Rule("sys.repo-scripts", APPROVE, "A script of the agent repo that acts (anything in scripts/ that is not a trusted reader)",
         "This script acts with the agent's own credentials.", text=None, script=("*repo-script*",)),
    Rule("sys.chmod-recursive", APPROVE, "Change permissions or ownership of a whole tree (chmod -R, chown -R)",
         "Changes many files at once.", prog=("chmod", "chown", "chgrp"), args=r"(^|\s)-[A-Za-z]*R"),
    Rule("sys.disk", APPROVE, "Write raw disks or images (dd, mkfs, newfs)", "Can overwrite a disk.", prog=("dd", "mkfs", "newfs", "asr")),
)

# ═════════════════════════════════════════════════════════════════════════════
# 6. HERMES' OWN TOOLS (not the shell). Anything not listed runs freely.
# ═════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ToolRule:
    id: str
    action: str
    title: str
    why: str
    #: Exact tool names, or a prefix ending in `*`.
    tools: Tuple[str, ...]
    #: `args[key]` must match one of these (case-insensitive) for the row to apply. None = always.
    when_key: Optional[str] = None
    when: Tuple[str, ...] = ()
    #: …or must NOT be one of these.
    unless: Tuple[str, ...] = ()
    #: OMNI_BRIDGE_FREE may list this id to turn the row off (never for BLOCK rows).
    can_free: bool = False


#: Meetings (owner decision 2026-11-12, docs/omni/meetings.md): Omni attends a meeting only after
#: The owner approves attending it, and speaks in it only after he separately approves speaking.
#: The omni-meet plugin's tools are the only way in; Hermes' own google_meet tools never run.
MEET_DOOR_TOOLS = ("omni_meet_attend", "omni_meet_allow_speech", "omni_meet_say")

TOOLS: Tuple[ToolRule, ...] = (
    ToolRule("tool.send-message", BLOCK, "Hermes' own send_message to anyone but the owner's own chat",
             "Messages to other people are never sent directly. Draft it with omni_propose (kind message).",
             ("send_message", "react_to_message", "start_chat", "send_*")),
    # Owner report 2026-11-17: a clarify prompt flashed in the apps and the turn hung 10 minutes. The Omni apps
    # cannot show or answer Hermes' clarify; the question belongs in the reply, where the owner answers it.
    ToolRule("tool.clarify", BLOCK, "Hermes' clarify (a question that pauses the turn for an answer)",
             "The Omni app cannot show or answer a clarify prompt, so the turn would wait until it times out. "
             "Ask the question in your reply instead: end the turn with the question and the options, and "
             "The owner answers in the chat.",
             ("clarify",)),
    ToolRule("meet.upstream", BLOCK, "Hermes' own Google Meet tools (meet_join / meet_say)",
             "Omni joins and speaks in meetings only through omni_meet_attend / omni_meet_allow_speech, "
             "each approved by the owner in the Omni app. Propose an action with grants meet.attend (and meet.speak).",
             ("meet_join", "meet_say")),
    ToolRule("meet.attend", APPROVE, "Send Omni to a meeting (its address is added to the invite)",
             "Everyone in the meeting will see Omni join as a participant, and the other guests will see it on the "
             "guest list. It listens and keeps a transcript; it does not speak unless speaking is approved too.",
             ("omni_meet_attend",)),
    ToolRule("meet.speak", APPROVE, "Let Omni speak in a meeting when someone addresses it",
             "Omni will answer out loud, under the meeting rules (only when addressed by name, two sentences, "
             "nothing private), and every line it says is sent to the owner.",
             ("omni_meet_allow_speech",)),
    ToolRule("meet.say", APPROVE, "Say this exact sentence aloud in the meeting",
             "Everyone in the meeting will hear it.",
             ("omni_meet_say",)),
    # Matches no tool by itself: an approved `action` names it in grants to let browser_* calls through on an item
    # card's thread while the browser holds sign-ins (otherwise net.card-thread). tools=() never matches.
    ToolRule("browser.signed-in", APPROVE, "Use Omni's signed-in browser in this item card's conversation",
             "The browser is signed in to sites (Luma and others) as the owner or Omni; the steps run with those sign-ins.",
             ()),
    ToolRule("tool.execute-code", APPROVE, "execute_code (a Python program run outside the sandbox)",
             "This tool runs a program with the agent's full access: no sandbox applies to it. "
             "For scripts, write the file in the workspace and run it with the terminal — that needs no approval.",
             ("execute_code",), can_free=False),
    ToolRule("tool.cron", APPROVE, "Create, change or remove a scheduled job",
             "A job runs later, with nobody watching. (It will run under these same rules.)",
             ("cronjob", "cronjob_manage"), when_key="action", unless=("list", "status", "get", "show", "history", "run", "pause", "resume")),
    ToolRule("tool.skill-manage", APPROVE, "Create, edit or delete a skill",
             "Skills are instructions every Hermes surface follows later — Telegram and the desktop app too.",
             ("skill_manage",), can_free=True),
    ToolRule("tool.memory", APPROVE, "Write to long-term memory",
             "Memory is read back into every later conversation, on every surface.",
             ("memory",), when_key="action", unless=("read", "list", "search", "get", "view", "recall"), can_free=True),
    ToolRule("tool.browser-act", APPROVE, "Type into a web page, or script the browser (browser_type, browser_cdp, browser_exec, browser_dialog)",
             "Typing into a page, or scripting it, can submit a form as the owner wherever the browser is signed in.",
             ("browser_type", "browser_cdp", "browser_exec", "browser_dialog", "browser_press"), can_free=True),
    ToolRule("tool.computer-use", APPROVE, "Control the Mac's screen, keyboard and mouse", "Desktop control can do anything the owner can.",
             ("computer_use", "computer_*", "desktop_*")),
    ToolRule("tool.home", APPROVE, "Change something in the house (Home Assistant service call)", "Acts in the physical world.", ("ha_call_service",)),
    ToolRule("tool.kanban", APPROVE, "Hand work to other agent workers (kanban create / comment / complete …)",
             "Kanban workers are separate agent processes with their own rules.",
             ("kanban_create", "kanban_comment", "kanban_complete", "kanban_block", "kanban_unblock", "kanban_link", "kanban_attach",
              "kanban_attach_url", "kanban_request_review", "kanban_request_changes", "kanban_heartbeat")),
    ToolRule("tool.connections", BLOCK, "Change Hermes' connections or tool catalog", "This conversation cannot change its own tools.",
             ("manage_connections", "manage_catalog")),
)

#: MCP tools, by the verb in their name. Trusted servers (OMNI_BRIDGE_MCP_SERVERS,
#: default `parachute,prism`): reads and note writes run; deletes and bulk changes ask;
#: anything that sends is refused. Other servers: reads run, every write asks.
MCP_READ = r"(query|list|get|search|read|find|info|doctor|whoami|status|view|show|describe|lookup|fetch|export|resolve|count|vault-info|vault_info)"
MCP_OUTWARD = r"(send|post_|post-|tweet|publish|transfer|pay|invite|email|share|notify|broadcast|dispatch)"
#: Work unblock 9: a tool whose name STARTS with a read verb is a read, whatever noun follows
#: (`get_email`, `list_payment_intents`) — unless a later name segment is an unmistakable send verb.
MCP_READ_LEAD = r"^(get|list|search|read|query|find|fetch|describe|count|view|show|lookup|retrieve)([-_]|$)"
MCP_SEND_SEGMENT = r"(^|[-_])(send|publish|tweet|broadcast|invite|notify|transfer|dispatch|forward|reply|post|share)([-_]|$)"
MCP_DESTRUCTIVE = r"(delete|remove|prune|merge|purge|drop|bulk|batch|rename-tag|rename_tag|delete-tag|delete_tag|manage-token|manage_token|revoke|mint|grant)"
#: Names the destructive pattern catches that only make new content (an image without its background,
#: a new design merged from others): ordinary design work, not deleting records.
MCP_NOT_DESTRUCTIVE = r"(^|[-_])(remove[-_]background|merge[-_]designs?)([-_]|$)"
MCP_RULES = (
    ("mcp.outward", BLOCK, "An MCP tool that sends or publishes", "Sends go through an approval card. " + PROPOSE),
    ("mcp.destructive", APPROVE, "An MCP tool that deletes, merges or changes many records (vault delete / bulk / tags / tokens)",
     "Deletes or changes many records at once."),
    ("mcp.untrusted-write", APPROVE, "A write through an MCP server Omni does not treat as its own vault",
     "This server is not one of Omni's own stores; its writes may notify other people."),
)

ALL_SHELL: Tuple[Rule, ...] = SENDS + SECRETS + SELF + READERS + APPROVALS


def compiled(rule: Rule):
    """(args, unless, text) as compiled patterns, cached on the function."""
    cache = compiled.__dict__.setdefault("_c", {})
    if rule.id not in cache:
        cache[rule.id] = (_r(rule.args), _r(rule.unless), _r(rule.text))
    return cache[rule.id]


def all_rule_ids() -> Sequence[str]:
    return [r.id for r in ALL_SHELL] + [t.id for t in TOOLS] + [m[0] for m in MCP_RULES]


#: Approval-tier rows an approved `action` may NOT name: execute_code runs outside the sandbox
#: altogether (its row is never freeable either). BLOCK rows are never grantable by anyone —
#: sends, secrets and the guard itself have no "allow" at all.
NOT_GRANTABLE = ("tool.execute-code",)

#: APPROVE rows that reach other PEOPLE. Internal autonomy (OMNI_BRIDGE_AUTONOMY=internal) runs every
#: other approval-tier call without a card; these keep their card (owner rule: only outbound is gated).
OUTWARD_APPROVALS = frozenset({"mcp.untrusted-write", "sys.gh-notify"})


def grantable() -> "dict":
    """{rule id: title} of every APPROVE row an approved `action` (omni_propose kind action,
    payload.grants) may let through during its own turn, without a card per call."""
    rows = [(r.id, r.action, r.title) for r in ALL_SHELL] + [(t.id, t.action, t.title) for t in TOOLS] + list((m[0], m[1], m[2]) for m in MCP_RULES)
    return {i: title for i, action, title in rows if action == APPROVE and i not in NOT_GRANTABLE}
