# Omni tool policy — full power, guarded edges

What the Hermes agent may do when the owner talks to it through the Omni app,
and what stops it at the edges. The code is `hermes/plugins/omni-bridge/`; the
rules are ONE table, `guard_table.py`; every row has a test in
`tests/test_omni_policy.py` (the test fails for a row with no case, and for a
row missing from this page).

This policy applies to **Omni turns only** — turns that arrive through Hermes'
API server from the Prism Server's Omni gateway, the sub-agents those turns
start, and the cron jobs they create. Telegram, the desktop app, the CLI, Buzz
and the cron jobs those surfaces created are untouched
(`tests/hermes_verify_omni_bridge.py` checks that against real Hermes code).

---

## Owner-authorized internal autonomy (2026-10-10)

The owner has requested internal work without routine permission prompts, retaining exact approval for outbound communications. `OMNI_BRIDGE_AUTONOMY=internal` enables that mode for Omni turns and inherited jobs/delegates. Without that exact value, the guarded mode documented below remains unchanged.

Internal filesystem work, research/network, installs, service operations, memory, skills, recurring jobs and vault mutations no longer create command-approval cards. The existing credential/guard exclusions remain: arbitrary code uses the contained terminal rather than the uncontained `execute_code` tool. The mode does not expose raw credentials, disable authentication, expand another user's vault permissions, or grant an agent permission to rewrite its own boundary. On a host where containment is unavailable, the existing explicit fallback still applies.

Email, Matrix/chat messages, tweets, invitations and RSVP retain the exact-payload proposal/approval path. A scheduled job may produce drafts; creating the schedule is not permission to send future drafts. Sending executors must be configured independently. Existing wallet signing protections are unchanged.

This is a trusted-owner autonomy policy, not a proof that arbitrary code or browser automation can never communicate: those general tools have effects a command-name classifier cannot completely infer. Known sending tools and credential-bearing paths remain guarded, credentials stay with their configured integrations, and the model is explicitly instructed to use the approval executor for all communications. Do not describe heuristic command matching as a complete network reference monitor.

Activation and rollback use the named runbook, after policy and hook tests pass. Never edit the protected runtime env from an Omni turn.

## The one door on EVERY Hermes turn (2026-10-15)

Owner requirement: nothing is sent to another person (email, chat message,
tweet, calendar invite/RSVP) unless the owner approved that exact message in the
Omni app from a signed-in device. The rest of this document is the policy of an
**Omni** turn. Every **other** Hermes turn — the Telegram gateway, the desktop
app, the CLI, a cron job that was not created from Omni — now gets the outward
edge of it, and nothing else (`policy.decide_outward`, tested in
`tests/test_guard_all_turns.py`):

| Refused on every turn | How it is recognised |
|---|---|
| Sends from the shell | every row of § 1 below (proton_send.py, gog gmail send, gog calendar respond / invite, twitter_post.py, omni_tweet_send.py, matrix_reader.py send/send-to, chat APIs, smtp from curl or code, wallet/Safe writes, dispatch_task.py / meeting_followup.py, `hermes send`, AppleScript/Shortcuts sends, an `osascript <file>` run — including a file written moments earlier, `printf … > s.scpt && osascript s.scpt` — and `open imessage:/sms:/facetime:/tel:/…`) |
| Any `gog` / `gws` line that is not a known READ — and any `gws_bridge.py` line (review round 6) | off Omni gog is read-only (review round 1, B1); Hermes' google-workspace `gws_bridge.py` (which runs `gws <args>` with the Hermes Google token) is held to exactly the same read list (`send.gws-bridge`), however it is run (`python3 …/gws_bridge.py`, `./gws_bridge.py`, `python3 -m gws_bridge`, `python3 - … < gws_bridge.py`): calendar/cal events·list·get·freebusy·calendars·colors·search·agenda, gmail/mail/email search·get·thread(s)·labels·list·attachment·messages list/get/search, drive ls·list·search·get·info·download·export, docs cat·get·export·info, sheets get·metadata·values get, contacts/people list·search·get, tasks list·lists·get, auth list, `--help`, `version`. A flag before the subcommand must be `--x=y`, a known boolean (`-j`, `--json`, …) or a known value flag; any other pre-subcommand flag makes the line not a known read (closes the `gog --select gmail --select search send …` bypass, review round 2). Every alias of a send (`gog send`, `gog mail|email send`, `gog cal respond`, `gog chat … send`) is refused — and so is every other gog write (a calendar hold without attendees, a Drive upload, a task added): those are done from Omni or by the owner. |
| Starting an unguarded Hermes | `hermes send`, `hermes chat`, `hermes -z/--oneshot`, `hermes-acp`, `python -m hermes_cli[.main] send|chat`, a `hermes_cli/main.py` path, `uv run|uvx|pipx run hermes send|chat`, and any `hermes` with `HERMES_HOME=` (a home that loads no guard) |
| Deciding for the owner | shell or code text that calls `/api/omni/approvals/<id>/decision|decide|send-grant` or Prism's `/api/actions/…` — matched after percent-decoding and normalising `/./`, `//`, `%2F`, and whenever `approvals` and `decision|decide` both appear even assembled from fragments (review round 2) — plus Prism's sign-in / device / password endpoints (`/auth/request`, `/auth/callback`, `/auth/set-password`, `/auth/change-password`, `/auth/device/…`, `/auth/devices`, `/auth/verify|magic|login|logout` — any other `/auth/` text is fine, review round 3 M4) and the Prism origin on this machine: port 8787 on any loopback spelling (`127.0.0.1`, `127.1`, `0x7f000001`, `localhost`, `::1`, `0.0.0.0`), this host's own name (`$(hostname)`, `$HOSTNAME`, `*.local`). Port 8787 is Prism's here, so `wrangler dev` on `localhost:8787` is refused off Omni too (accepted trade-off: run it on another port, or from Omni). Matched after undoing cheap shell disguises — empty quote pairs (`/au''th`) and `$VAR`/`${VAR}` set on the same line (`P=8787; … :$P`) — which could mint a credential that approves sends; and reading Prism's database or `.env` (`prism-server*.db[-wal|-shm]`, `apps/server/.env`, `~/.prism`) — where the session ids live (review round 1, B3) |
| Credential reads (every turn, S2) | `paths.is_secret` places named by a shell command (`cat`, `less`, `grep`, `cp`, `source .env`, `sqlite3` …), by `read_file` / `search_files`, or inside `execute_code` / `python -c` text: any `.env`, `~/.hermes/.env` and `auth.json`, Keychains, `~/.ssh`, gog/gh/gcloud stores, `.pem/.key/.p8`, Prism's DB and env, `~/Library/Messages` and `~/Library/Mail`. Exception: this repo's `state/` history stays readable except its credential files (`proton-mail-state.json`, `matrix-sync-state.json`, `pending-approvals.json`, `*identity*`, `*token*`, `*credential*`, `*secret*`, `*session*`, `*password*` — one predicate, `paths.state_secret`, shared with Omni turns since work unblock 2026-10-22). A wildcard is caught too (review round 2): a glob whose fixed directory is a secret place or the Prism server dir, or whose basename matches a distinctive secret name (`.env`, `prism-server*`, `id_rsa`, `*.db`/`*.sqlite`) — so `prism-server.d?`, `*.db`, `.e*v`, `.e''nv` are refused (`*.db`/`*.sqlite` anywhere is over-refused; a turn rarely cats a database). The repo's own scripts still run — they read `.env` themselves; their command lines name no secret. |
| A Telegram note to anyone but the owner | `send_telegram.py --chat…/--to` or a `TELEGRAM_CHAT_ID=` prefix (the script is pinned to his chat anyway) |
| Hermes' own `send_message` / `react_to_message` / `start_chat` | allowed only for `target` = `telegram` (home channel = his chat, verified 2026-10-15) or `telegram:<OMNI_OWNER_TELEGRAM_CHAT>[:topic]`; `action: list` is allowed; a call with NO target is left to Hermes, whose tool refuses it ("Both 'target' and 'message' are required" — checked in `tools/send_message_tool.py` on the agent host, 2026-10-15). Cron deliveries do not use the tool (`cron/scheduler.py` `_deliver_result`), so they are unaffected. |
| Outward MCP tools | the `mcp.outward` verb rule |
| Sender code | `execute_code`, `write_file` / `patch`, `python -c` content matching the § 1 text rows (smtplib, X API, chat APIs), naming a send script, or running `gog`/`hermes` sends — e.g. `subprocess.run(['gog','send',…])`, `os.system('hermes send …')` (review round 2). A path or URL assembled by string concatenation (`'.e'+'nv'`, `'deci'+'sion'`) is collapsed before matching. |
| Credential dumps | every row of § 2 (Keychain, other processes' environments, token minting, `hermes config/auth`) |
| Switching the door off | `launchctl setenv/config`, writing the plugin, `~/.hermes/config.yaml`, `~/.hermes/.env`, `~/.hermes/plugins`, LaunchAgents, the repo `.env`/`launchd/`, the send scripts and `scripts/send_grant.py`, or code the guard process itself loads (`scripts/proactive_intelligence/`, `scripts/gen_capabilities.py`, `config/omni-capabilities.yaml`) |

Every refusal names the way: the `omni_propose` tool, or — where a surface does
not offer the tool — `python3 scripts/omni_propose.py <kind> …`. Both only
PROPOSE; the draft lands in **Needs you**.

What does NOT change on those surfaces: no sandbox, no approval cards, no
write-outside-workspace rule. Those would break ordinary Telegram/desktop work
(editing code, running scripts), so they stay Omni-only. The credential-read
rule DOES apply on every turn (above). The door holds whether or not the bridge
is configured (`OMNI_SERVICE_TOKEN` / a loopback gateway): only a Buzz relay
process (`OMNI_HERMES_GUARD=buzz`) skips it, because omni-buzz-guard governs it. A fault in the outward check blocks only a
call that looks outward; anything else runs, so a bug cannot brick Telegram.

`omni_propose` is registered for every turn of a configured process. Off Omni it
is sent with **no** `threadId` (a Telegram chat id is not an Omni thread).

**Deploy step (Hermes config, by the owner):** Telegram offers the tool only if
its toolset list includes `omni`:

```yaml
platform_toolsets:
  telegram:
    - hermes-telegram
    - omni
```

Until then the Telegram model is pointed at `scripts/omni_propose.py`.
`approvals.mode: 'off'` stays as it is: the one door does not use Hermes'
approval prompts.

**Top residual: Google sends have NO server-side stop.** A Gmail send, a
Calendar event that invites or updates attendees, or a Drive share made with the
Hermes Google token (`~/.hermes/google_token.json`, scopes gmail.send, calendar,
drive) never touches Prism: google_api.py has no send-grant check, and Google
does not ask. On a non-Omni turn the ONLY thing between that token and a send is
the text guard below (`secret.path` for the token file, `send.google-api*`,
`send.gws-bridge`, `send.google-rest`, `send.code`), and text matching can be
defeated — for example `getattr(svc.users().messages(), 'se'+'nd')(…)` inside
code that obtained a Google service some way the guard did not see. Review round 6
found these further text-guard evasions, all still open off Omni (each is
documented, none is fixed by a text rule):

- **Globs and directory listings in code** read the token without naming it:
  `glob.glob(os.path.expanduser('~/.hermes/*.json'))`, `os.listdir('~/.hermes')`,
  `(Path.home()/'.hermes').glob('g*.json')`, a reversed string.
- **Recursive and archive readers** sweep it up: `grep -r refresh_token ~/.hermes`,
  `tar cf - -C ~ .hermes | base64` (these also dump `~/.hermes/.env` and `auth.json`).
- **Shell expansion** assembles the name or the URL at run time: brace expansion
  (`~/.hermes/{google_token,x}.json`), variables (`a=google_; …/${a}token.json`),
  command substitution `$(…)` (`$(echo google_toke)n.json`, `…/messages/$(echo send)`),
  `ls | grep | xargs -I{} cat`, and `echo 'gmail send …' | xargs python3 /tmp/g.py`.
- **`curl -K <config file>`**: a Calendar write whose method and query sit in a curl
  config file, not on the command line.
- **Request parts in a file**: a `POST https://www.googleapis.com/batch` whose parts
  (any Gmail or Calendar call) sit in `--data-binary @batch.txt`.
- **Escapes**: `messages/s\end` (the shell drops the `\`), `$'se\x6ed'`, and in code
  `'%s' % 'send'` or `'/'.join([…, 'send'])`.
- **Copies of google_api.py by glob or folder** (`cp …/google_ap?.py`, `cp -r scripts`,
  `rsync scripts/`), then run with the send group in a variable (`S=gmail; python3
  /tmp/g.py $S send`).

Together, one `execute_code` can glob the token, refresh it and POST to
`…/messages/%s` % 'send' — a full send that names nothing the guard knows.

**The Google token must be issued read-only (gmail.readonly, calendar.readonly,
drive.readonly) BEFORE it is ever signed in on the agent host — or Hermes must run as a
separate macOS user that cannot read it.** Until one of those is done, do not sign
Google in on the agent host: the text guard is not a stop. The real fix, not yet done:
re-issue the Hermes Google token with read-only scopes (gmail.readonly,
calendar.readonly, drive.readonly), or run Hermes as a separate macOS user that
cannot read the token at all; failing that, make google_api.py's send and
calendar-write functions require the Prism send grant like every other sender.
Whether the token is present on the agent host has not been verified.

**Honest limits.** Non-Omni turns have no OS sandbox. Matching a command line
or code text is best effort: an obfuscated script written and run in two steps
can still reach SMTP, the X API or Matrix with credentials it reads itself
(`base64 -d | sh`, a file written by a non-guarded editor, a program compiled on
the spot). The credential-read rule matches paths a command line or code text
names; a path assembled at run time is not seen. Off Omni there is no network
rule either, so readable non-secret data can still be posted to a URL. For
the repo's own senders (Proton, X, Matrix, Telegram) the hard stop is in the
senders — each refuses without Prism's one-time grant (`scripts/send_grant.py`).
That is NOT true for Google (see the top residual above). What remains is a process that
reads the raw credentials (Keychain, `.env`) and speaks the protocol itself —
including direct IMAP/SMTP to Proton Bridge from inline code
(`imaplib.IMAP4('127.0.0.1', 1143)`), which text rules do not stop; the hard
stop for sends is the grant check plus the credentials. The Prism-origin rule
undoes only same-line shell disguises: a value computed at run time or split
across terminal calls is not seen — for Prism itself the server side
(device-only approval and sign-in, review rounds 2–3) is the real stop.

**Buzz `omni` agent.** buzz-acp answers every ACP permission request with
`allow_once`. The omni agent (Claude Code via claude-agent-acp) now runs its
bundled CLI through `scripts/buzz_omni_claude` (`CLAUDE_CODE_EXECUTABLE` in
`scripts/buzz_agents.py`): `--permission-mode dontAsk` plus the
`scripts/agent_claude.py` allowlist/denylist, so a tool that is not allowed is
denied without any permission request. The Hermes Buzz agents keep
`omni-buzz-guard` (default deny).

## For the owner: the rules in plain language

Omni has the same tools the Telegram agent has: the terminal, files, code, the
web, the browser, memory, skills, scheduling, sub-agents, the vault.

1. **Nothing is sent as you without you seeing the exact draft.** Email, a
   message to another person, a calendar invite, a post, anything from the
   wallet: the agent cannot send these. It writes a draft and a card appears
   in the app. (Today the card then says the sender is switched off, because
   it is — `OMNI_EXECUTORS=off`.)
2. **Your passwords and tokens cannot be read.** Not with `cat`, not with a
   script, not in base64, not through the browser. Shell commands run in a
   box the operating system enforces; inside it the credential files do not
   open and the Keychain does not answer.
3. **The shell cannot reach the internet, or other programs on the Mac, on
   its own.** The agent's own read commands (your calendar, mail search,
   tasks, GitHub reads) work normally. Hermes' web search and browser work
   normally. Anything else that wants the network asks you first.
4. **It can write freely in one folder** (`~/omni-workspace`, plus the repo's
   `outputs/`) and in temporary folders. Writing anywhere else asks you first.
5. **Risky things ask you first**: installing software, restarting a service,
   deleting a folder, `git push`, changing your calendar or Drive, scheduling
   a job, typing into a web page, running code
   outside the box. Memory and skill writes are free under
   `OMNI_BRIDGE_FREE=memory,skill-manage`; secrets and the guard stay protected.
6. **Asking means a card in the app** with the exact command, the folder it
   would run in, and why it was stopped. **Approve Once** runs exactly that.
   **Deny**, or no answer for ten minutes, and it does not run. The card
   cannot be edited, and an approval is good for that one command only.
7. **The agent cannot switch any of this off.** It cannot edit the plugin,
   Hermes' settings, the files that start the services, or its own rules —
   not even with your approval. Changing the rules is something you do, at
   the agent host. And if the guard itself is ever missing or broken, Omni
   does not get a free hand: it gets no terminal and no file tools at all.
8. **One line turns it all back** to today's short tool list (see the
   runbook, "Rollback").

---

## How it works: three layers

```
 a tool call of an Omni turn
        │
        ▼
 1. THE TABLE (guard_table.py, policy.py, shell.py)        decides the ROUTE
        │   block ──► refused, told to use omni_propose
        │   approve ─► a card in the app; waits; default is "no"
        │   reader ──► runs, with exactly the access its row grants
        │   (anything else) ─► runs
        ▼
 2. THE SANDBOX (sandbox.py — macOS sandbox-exec)          decides what it CAN do
        │   every shell command, whatever it is, runs inside it
        ▼
 3. REDACTION (redact.py)                                  cleans what comes BACK
            a known credential value in any tool result is replaced
```

Layer 2 covers more than the terminal. Hermes' file tools (`read_file`,
`search_files`, `write_file`, `patch`) do their work as shell commands on the
turn's terminal environment; the plugin wraps that environment, so those
commands run in the same sandbox. A tool that opens a file in Hermes' own
process instead (`vision_analyze`, a `file://` address) is handed a private
copy that was read inside the sandbox, and one that writes a file
(`text_to_speech` with an output path) writes to a private place from which
the sandbox delivers it. This matters because a path can be judged as one
thing and opened as another — a symlink swapped in between the two. No file
access of an Omni turn rests on a path rule alone (`contain.py`).

Layer 1 reads the command. Layer 2 does not need to: it is the kernel refusing
a file open or a connection, so it holds for a command the reader did not
understand, for a script, and for a child of a child. **A command that gets
past the reader still cannot read a secret, write outside the workspace, reach
the network, or make another program act for it.** The table exists to give a
good answer early ("draft it with `omni_propose`") and to decide which
commands get MORE than the default, never to be the only thing in the way.

### What a shell command gets (one of three tiers)

| Tier | When | Reads | Writes | Network | Keychain | Other programs |
|---|---|---|---|---|---|---|
| **contained** | every command, by default | everything except secrets | the workspace and temporary directories | none — not even this machine (ports in `OMNI_BRIDGE_LOOPBACK_PORTS` excepted) | no | none: no launchd jobs, no AppleEvents, no app launches, no clipboard, no local sockets, a short list of system services only |
| **reader** | the whole command is a trusted read from the table (or one piped only into file-less filters / saved by one `>` into the workspace) | + what its row names (e.g. `gog`'s own token store) | as contained | if its row grants it | if its row grants it | as contained, system services open |
| **approved** | the owner approved this exact command | everything except secrets (+ its own credential if it is `git`/`ssh`/`gh`/`pm2`/`gog` or a repo script, and is the whole command) | anywhere except the guard's own files and the secrets | yes | only as above | no — unless the command IS `launchctl` / `osascript` / `open` … (rows `sys.services`, `sys.automation`) |

A trusted `reader` or `approved` command whose row explicitly grants Keychain access also receives `file-read-data` on the two exact canonical standard login files (`~/Library/Keychains/login.keychain-db` and the legacy `login.keychain`). macOS SecItem queries require this permission on the encrypted backing database as well as SecurityServer access; without it, gog's [99designs/keyring dependency](https://github.com/99designs/keyring/blob/master/keychain.go) reports a masked “not found” when SecItem returns no results. No directory-wide access, other Keychain store, file write or `/usr/bin/security` execution is granted. Contained and non-Keychain readers keep the denial. The raw-secret command/file policy is unchanged. A disposable synthetic Keychain fixture exercises SecItemCopyMatching and these negative boundaries without reading real credentials.

In every tier the environment is rebuilt from a short list of harmless names
(`HOME`, `PATH`, `LANG` …): no `*_KEY`, `*_TOKEN`, `*_SECRET` reaches a
command, and a name that looks like a credential cannot be added to the list.
Setuid programs (`sudo`, `ps`, `crontab`, `at`) do not start inside the
sandbox at all.

If `sandbox-exec` does not work on a machine, the plugin says so in the log
(`sandbox UNAVAILABLE`) and **every** shell command and file-tool call asks
for approval instead.

The guard's own files and the secret stores are unwritable in every tier,
including when one of them happens to sit inside a writable directory.

---

## The guard table

`block` = refused, nobody can approve it from the app. `approve` = a card.
`reader` = runs without asking, with the access shown.

### 1. Outward sends — always a draft card, never a direct run

The refusal names the legitimate path: `omni_propose` with the kind shown.

| Row | What it catches | Draft kind |
|---|---|---|
| `send.proton` | Email as the owner (`proton_send.py`) | email |
| `send.smtp-tools` | Mail from the shell (`sendmail`, `mail`, `msmtp`, `mutt`, `swaks`) | email |
| `send.smtp-curl` | Mail through curl (`smtp://`, `--mail-rcpt`) | email |
| `send.smtp-code` | Mail from inline code (`smtplib` and friends) | email |
| `send.gog-gmail` | Gmail send / reply / forward / `fwd` / `autoreply` / draft send-create-update / `messages send|insert` (`gog`, also `gog mail|email …`, `gws gmail … send`, `+send`, and a top-level `gog send`). Judged on gog's COMMAND words (work unblock 2026-10-22): a sheet cell or doc body that says "email … reply" is not a send; a head gog cannot be read past (an unknown flag before the subcommand) is refused | email |
| `send.gmail-settings` | Gmail settings that send mail to other people or hand the mailbox over: (gws too: `gws gmail users settings …` any change but get / list, and `filters` changes whose body mentions forwarding — fix round 2) forwarding, auto-forward, the vacation responder (consistent with `autoreply`), send-as, delegates (`gog gmail [settings] forwarding / autoforward / vacation / sendas / delegates …`), and a filter create / update with a `--forward…` action. Listing / `get` runs. Command words; review fix round 1 | email |
| `send.calendar-rsvp` | An RSVP to an exact Google event/occurrence (`gog calendar|cal respond`, aliases `rsvp` / `reply`), regardless of notify flags; command words only | calendar-rsvp |
| `send.calendar-invite` | A calendar event write (`create / add / new / update / edit / set / move / delete …`) that notifies other people: an `--attendees` / `--add-attendee` / `--notify` flag, or `--send-updates` other than `none`. Command words and real flags only, never text inside a value | calendar-invite |
| `send.calendar-decline` | Focus time / out of office (`focus-time`, `focus`, `out-of-office`, `ooo`) unless `--auto-decline none` (gog's default `all` declines every overlapping invitation and notifies its organizer); an event write with `--event-type focus-time|out-of-office` or a `--focus-auto-decline` / `--ooo-auto-decline` other than `none`; any `propose-time`. The same commands with `none` are `sys.calendar-write` | message |
| `send.gog-chat` | Any Google Chat command that is not a read (`gog chat spaces|messages|threads|dm list / get / search` are reads with gog's credential) | message |
| `send.google-share` | Sharing a Google file with someone (`gog drive / docs / … share`, `permissions create / add / update`); unshare and `permissions delete / remove` run as usual | message |
| `send.google-groups` | Adding or inviting someone to a Google Group (`gog groups … add / insert / invite`); listing runs as usual | message |
| `send.google-classroom` | Any Google Classroom change (`gog classroom` / `gog class` … anything but `list` / `ls` / `get` / `info` / `show` / `search`): nearly every one notifies students, teachers or guardians | message |
| `send.google-comment-mention` | A Docs / Drive comment (`comments add / create / new / reply / respond`) whose text mentions someone (`@name`, `+email`, an email) or that uses `--assign` / `--mention`; a plain comment runs as usual | message |
| `send.keep-share` | Sharing a Keep note (`gog keep … share`, `collaborators add`) | message |
| `send.hermes-cli` | Hermes' own `hermes send` (Telegram / Discord / Slack / Signal / SMS with the gateway's tokens) | message |
| `send.hermes-meet` | `hermes meet …` / `hermes omni-meet …` other than status, transcript, setup or help: joining, speaking or signing in to a meeting from the shell. Use `omni_meet_attend` / `omni_meet_allow_speech` (an approved action) | omni_propose kind `action` |
| `send.applescript` | A message sent by scripting an app (`osascript` naming Mail, Messages, Outlook, Slack, Telegram, WhatsApp, Signal, Discord, or `send`; a script file's content is read off Omni) | message |
| `send.shortcuts` | Running a Shortcut (`shortcuts run`), which can send as the owner | message |
| `send.google-api` | Gmail send/reply/forward, every drafts … send, ANY calendar create/insert/update/patch/import/respond (with or without attendees, review round 4), or a Drive share through Hermes' google-workspace skill (`google_api.py`) — every turn (review round 3). Reads still run. Off Omni the same argument shapes are refused for ANY script an interpreter runs (a copy, a symlink, a renamed file; review round 5). In inline code: `….messages().send(`, `messages.send`, `.drafts().send(`, every `events().insert|patch|update|import_|move|quickAdd(`, `sendUpdates='all'`. | email / calendar-invite |
| `send.gws-bridge` | Off Omni, any `gws_bridge.py` command that is not one of gws's read forms (`gmail +send`, `gmail users messages send`, `users drafts send`, `calendar +insert`, `calendar events insert` with attendees or without, `drive permissions create` …) — review round 6. The same gws send shapes (`+send`, `+reply`, `+forward`, `+insert`, `messages|drafts send`, `events insert|patch|update|import|move|quickAdd`, `permissions create`) are refused for ANY script an interpreter runs, so a renamed copy is caught when it sends. | email / calendar-invite |
| `send.tweet` | Post to X (`twitter_post.py`) | tweet |
| `send.tweet-api` | Post to X through its API | tweet |
| `send.wallet` | Wallet / Safe / on-chain write (`wallet.py propose-transfer` …) | wallet-proposal |
| `send.wallet-tools` | On-chain write from the shell (`cast send`, `safe-cli`) | wallet-proposal |
| `send.wallet-safe-script` | Create or change a Safe (`create_safe.py`) | wallet-proposal |
| `send.wallet-code` | Signing or sending a transaction from inline code | wallet-proposal |
| `send.chat-api` | A message to other people through a chat API (Telegram, Matrix, Slack, WhatsApp, Discord, Signal) | message |
| `send.chat-scripts` | A message to other people through a repo script (Matrix, dispatch) | message |
| `tool.send-message` | Hermes' own `send_message` / `react_to_message` / `start_chat` | message |
| `tool.clarify` | Hermes' `clarify` (pauses the turn for a multiple-choice answer). The Omni apps cannot show or answer it, so a turn hung until `clarify_timeout` (owner report 2026-11-17). Omni asks in its reply instead; `clarify` is also left out of `omni-full` (`OMNI_UNANSWERABLE_TOOLS`). Off Omni turns (Telegram has buttons) it is untouched | (none: ask in the reply) |
| `mcp.outward` | An MCP tool whose name says it sends, posts, publishes, pays, invites or shares. A name that STARTS with a read verb (`get` / `list` / `search` / `read` / `query` / `find` / `fetch` / `describe` / `count` / `view` / `show` / `lookup` / `retrieve`) is a read whatever noun follows (`get_email`, `list_payment_intents`, `get_post_comments`), unless a later segment is a send verb (`get_and_send_report`) — work unblock 2026-10-22, on every turn | (the matching kind) |
| `tool.outward-name` (built in) | Any other tool whose name says it sends | (the matching kind) |

GitHub work is internal (owner policy 2026-11-17): `git push` / `pull` / `fetch`
get gh's credential (an https remote authenticates through `gh auth
git-credential`) and `gh` changes run (`sys.gh-write`). Comments and reviews
talk to people and keep their card (`sys.gh-notify`). Connector writes are free
except comments and replies, which ask (`mcp.untrusted-write`); publishing and
sharing are `mcp.outward`. Under internal autonomy every OTHER approval-tier row
runs without a card; `guard_table.OUTWARD_APPROVALS` (`sys.gh-notify`,
`mcp.untrusted-write`) keep theirs. Before 2026-11-17 autonomy waved them through
too.

### 2. Secrets — never readable, never printable

| Row | What it catches |
|---|---|
| `secret.path` (built in) | Any command or tool that names a credential file or a private store: `.env` files anywhere, SSH and GPG keys, `~/.aws`, `~/.config/gh`, `gog`'s token store, the Keychain files, browser profiles and cookies, `~/.pm2`, `~/.parachute`, Hermes' `config.yaml` / `auth.json` / sessions / `state.db`, the CREDENTIAL files in the repo's `state/` (the Bridge TLS pin `proton-mail-state.json`, Matrix sync state, legacy `pending-approvals`, Buzz `*-identity.json`, anything named token / credential / secret / session / password, in any sub-folder — the rest of `state/` is ordinary agent state since work unblock 2026-10-22, readable and, outside the contained tier, writable; install receipts stay unwritable), Prism's database and `.env`, shell and REPL history files, anything in `OMNI_BRIDGE_SECRET_PATHS`. NOT secrets (work unblock 2026-10-22): the public CA bundles HTTPS needs (`/etc/ssl/cert.pem`, `certifi/cacert.pem`, Homebrew's `ca-certificates` / `openssl@*/cert.pem`, brew's `api/homebrew-N.pem`) and library source named after cookies (`http/cookies.py`, `requests/cookies.py`) |
| `secret.value` (built in) | Any tool call whose arguments contain the VALUE of a known credential |
| `secret.keychain` | `security find-…`, `dump-keychain`, `export` |
| `secret.process-env` | Other processes' environments (`ps e`, `/proc/…/environ`) |
| `secret.launchctl-env` | launchd's environments (`launchctl print` / `getenv` / `export` / `procinfo`) |
| `secret.pm2-env` | pm2's process environments (`pm2 env` / `jlist` / `describe` / `dump`) |
| `secret.hermes-config` | `hermes config` / `auth` / `secrets` |
| `secret.browser-session` | A browser with the owner's sign-ins: `agent-browser --auto-connect` / `--cdp` / `connect` (a running Chrome), `--profile` that is a Chrome profile name or a path outside the workspace, `--state` / `--config` / `state load` from outside it, `--restore` / `--session-name` (saved sign-ins), `auth …` (the credential vault), `-p` / `--provider` (cloud browsers), or any of those through `AGENT_BROWSER_*` / `env` |
| `secret.token-mint` | Minting or printing a credential (`parachute auth`, `gh auth token`, `gh auth status … -t / --show-token` anywhere on the line, `gcloud auth print-…`, `op read`). `--help` (or a final `-h`) on the CLI itself is help, not a credential, and runs (also for `secret.hermes-config`) |

Behind the rows, the sandbox: the same files do not open for any program an
Omni command starts, the Keychain does not answer, other processes'
arguments are not visible. Behind that, redaction: a known credential value
in any tool result is replaced with `[redacted by omni-bridge]`.

### 3. The guard itself — cannot be changed from inside, approved or not

| Row | What it catches |
|---|---|
| `self.files` (built in) | Writing an install receipt under the repo's `state/` (a rollback restores from it), the plugin, anything under Hermes' home, the repo's `hermes/` `scripts/` `launchd/` `config/` `venv/` `.env` `.git/hooks` `.git/config` `.claude/`, `~/Library/LaunchAgents`, shell start-up files, `~/.gitconfig`, `~/.ssh`, `~/.pm2`, the binaries credentialed programs run from (work unblock 2026-10-22, `paths.trusted_binaries`: `/opt/homebrew/bin`, `/opt/homebrew/sbin`, `/usr/local/bin`, `~/.local/bin`, and the directory each of `gog`, `gws`, `gh`, `git`, `ssh`, `rsync`, `pm2`, `agent-browser`, `claude`, `node`, `python3`, `hermes` really lives in — also on every non-Omni turn; consequence: `brew install` / `npm -g` cannot link new programs from an Omni turn, the owner runs those), anything in `OMNI_BRIDGE_GUARD_PATHS` |
| `self.skill-repo` (built in, **every turn**) | A `skill_manage` call whose target resolves outside `~/.hermes/skills`: a skill in the agent repo, in any `skills.external_dirs` entry, or reached through a symlink into either (real paths; a same-named skill anywhere counts, an unreadable external list fails closed). BLOCK on Omni turns even when `tool.skill-manage` is freed, and on every other Hermes turn. Repo skills change only through the repo; lessons go to `memory` or a local skill under `~/.hermes/skills`. |
| `self.os-schedule` | Scheduling with the operating system (`crontab`, `at`, `batch`) — use the cronjob tool, which runs under this policy |
| `self.launchd-env` | Changing the environment services start with (`launchctl setenv` / `unsetenv` / `config`) |
| `self.sandbox` | Leaving or rewriting the sandbox (`sandbox-exec`, `csrutil`, `spctl`, `tccutil`, `dscl`) |
| `self.setuid` | Programs macOS will not start in the sandbox (`sudo`, `su`, `doas`, `ps`, `top` are setuid) — told to use `pgrep -l` and `netstat -anv -p tcp` (listening ports with process name and pid). Other processes' details stay closed: `lsof` needs `process-info-pidinfo` on them, which also re-opens their argv and environment, so it sees only the command's own processes (work unblock 2026-10-22: not lifted). The sandbox denies `kern.procargs*` by PREFIX — the exact-name rule it had never matched (the kernel names it `kern.procargs2.<pid>`), so until then any command could read a same-user process's arguments and environment |
| `tool.connections` | Changing Hermes' connections or tool catalog (off Omni turns too, since Hermes v0.23 enables the toolset everywhere) |
| `meet.upstream` | Hermes' own `meet_join` / `meet_say`. Omni enters meetings only through `omni_meet_*` (docs/omni/meetings.md) |

A `self.files` refusal of a file in the production checkout is not a dead end: Omni changes its own repo in a clone in the workspace and ships it through `scripts/omni_ship.py` (skill `omni-self-improve`). Only a change to `omni_ship.CORE` comes to the owner, as a pull request.

### 4. Trusted readers — run without asking

Only when the reader is the **whole** command (no `&&`, no `$( )` beyond
`date`/`echo`) — or (work unblock 2026-10-22) when its output is only FILTERED or SAVED:
`[cd D &&] READER [| FILTER]… [> path]`, where each filter is one that cannot open a file
(`jq` without `-f` / `--rawfile` / `--slurpfile` / `-L` / file operands, and with no `import`,
`include`, `env`, `$ENV`, `input_filename`, `getpath`, `$__loc__` or other `$__…`; `head`, `tail`,
`wc`, `cut`, `tr`, `column`, `uniq` with no file operand; `sort` without `-o` / `-T`; `grep`
without `-f` / `-r` / a file operand; `sed` with only `p` / `d` / `q` / `s///` scripts and no
`-i`). Review fix round 1: `awk` and `cat` never carry a credentialed stream (an awk `BEGIN{ARGV…}`
opened the granted token store), `read.omni-nudge-sweep` keeps its service token only standalone,
and a `/dev/…` target (`/dev/tcp`, `/dev/udp`, `/dev/fd`) is not a place to save to. `2>&1` and `> /dev/null` anywhere, and
at most ONE output redirect, on the last stage, into the workspace or a scratch place (a relative
path only when the directory is known, and never for a repo script, which runs from the repo).
The grant is per sandbox, so every stage holds it: that is why the filters may open nothing.
Anything else in the line (`tee`, `sh`, `python`, `xargs`, `;`, `<`) drops the grant, as before.
The same shape keeps an approved transport's / repo script's / `sys.google-write`'s credential. Review fix round 2: every program of such a line — the reader, a wrapper it sits in, each
filter, a `$( )` command — must be TRUSTED BY LOCATION, not by name (`policy._exec_trusted`): a
name with a `/` must be a file nobody on an Omni turn can write (root-owned all the way up, or a
guard path) and resolve to one; a bare name must resolve to such a file on `paths.trusted_path()`,
Hermes' PATH minus every directory that does not exist or that an Omni turn could write (on the
the agent host: `~/.cargo/bin`, `~/.bun/bin`, `~/.npm-global/bin` sit before Homebrew). A credentialed command
RUNS with that PATH too. `./head`, `/tmp/x/jq`, `/x/gog`, a `gog` planted earlier on Hermes' PATH,
`env …` and a line that sets `PATH` keep no grant. Review fix round 3: "unplantable" is judged on the WHOLE chain (`paths._chain_safe`), for the
path as written and as resolved: the file must be root-owned all the way up or inside a guard place
that may be TRUSTED (the fixed bin directories, what their programs resolve to, the repo's venv,
Hermes' home — never a directory found only because a program came first on Hermes' PATH); and
every directory above it, up to one whose parent is root's alone, must itself be a guard path or an
ANCHOR. Anchors (`paths.anchor_paths`) are the ancestors of the trusted binary directories, of the
repo (`~/dev`, the repo itself), of Hermes' home and of `OMNI_BRIDGE_GUARD_PATHS`: each is protected
as a single path — not renamed, moved, removed or replaced (sandbox `literal` deny in every tier,
`self.files` in the policy) — while files inside it stay writable. So renaming `~/.local`,
`~/.npm-global`, `~/.hermes` or the repo and moving another tree in is refused. Long options of a filter never take the next
word (`grep --context[=n]` has an optional value): pass `--opt=value`.
A repo script counts only if it is this repo's `scripts/<name>` run with the repo's Python; it is
run from the repo.

| Row | Command | Gets |
|---|---|---|
| `read.gog-calendar` | `gog calendar events / list / get / freebusy / calendars` | network, Keychain, gog's token store |
| `read.gog-drive` | `gog drive ls / search / get / download`, `docs cat`, `sheets get` | same |
| `read.gog-gmail` | `gog gmail search / get / thread / labels` | same |
| `read.gog-auth-list` | `gog auth list` | Keychain, gog's store |
| `read.gog-any` | Any other gog / gws read: `forms get / raw / responses list`, `docs list-tabs / structure / raw / find-range`, `slides get`, `people me`, `keep list` … (generic `list / ls / get / info / show / search` in every other group) | network, Keychain, gog's token store |
| `read.omni-nudge-sweep` | `omni_cli.py sweep-owed [--commit] [--context-only]` | network, Keychain, repo `.env`, gog; only owner gateway proposals, never correspondence |
| `read.omni-cli` | `omni_cli.py tasks / calendar / open-loops` | network, Keychain, repo `.env`, gog |
| `read.omni-briefing` | `omni_cli.py morning / sense / evening / alerts` (without `--commit`) | + Claude's credential |
| `read.omni-tasks-write` | `omni_cli.py task-add`, `tasks done / drop` (The owner's own list) | network, Keychain, repo `.env` |
| `read.proton-search` | `proton_mail.py search / health / attachment / message` (read-only; sign-in mail refused) | network, Keychain, repo `.env` |
| `read.wallet` | `wallet.py balance / pending / history` | network, repo `.env` |
| `read.slots` | `find_slots.py`, `find_mutual_slots.py` | network, Keychain, repo `.env`, gog |
| `read.telegram-self` | `send_telegram.py` to the owner's own chat (no `--chat-id`) | network, repo `.env` |
| `read.gh` | `gh pr / issue / repo / run / release` list / view / status / diff / checks, `gh search`, `gh api` GET | network, Keychain, gh's store |
| `read.git-fetch` | `git fetch / pull / ls-remote` from the repository's own remote | network, Keychain, `~/.ssh` |

### 5. Needs the owner's approval — a card with the exact command

| Row | What it catches |
|---|---|
| `sys.browser` | Starting a SIGNED-OUT Chrome from the shell: `agent-browser`, `Google Chrome` / `chromium` / `chrome-headless-shell`, `playwright`, `npx playwright …`, `python -m playwright`, or any command prefixed `OMNI_NEEDS=browser ` (a node / python script that drives one). The sandbox additionally gets LaunchServices' lookup (app launching, AppleEvents, the clipboard stay closed), write access to Chrome's crash database (only its own settings and in-flight dumps are readable, never the owner's past reports or profiles), read/write of the agent's profiles under `<workspace>/.browser-profiles/`, and `AGENT_BROWSER_ARGS=--no-sandbox,--disable-crash-reporter,--disable-breakpad` (Chrome's own sandbox cannot nest; pass `--no-sandbox` yourself to a Chrome you start directly). No credential. Internal autonomy runs it without a card |
| `net.program` | A network program in the shell (`curl`, `wget`, `ssh`, `scp`, `rsync`, `nc`, `dig`, `ping`, `docker`, `aws`, `tailscale`, `open` …) — including to a port on this machine. Free only for a plain GET of a host in `OMNI_BRIDGE_EGRESS_ALLOW`, or a port in `OMNI_BRIDGE_LOOPBACK_PORTS` |
| `net.git-remote` | `git push / clone / remote add / set-url / submodule add` |
| `net.install` | Installing or downloading software (`pip`, `npm`, `npx`, `brew`, `uv`, `cargo`, `go` …) |
| `net.python-server` | `python -m http.server / pip / uvicorn / flask …` |
| `net.code` | Inline code that opens a connection (`import socket / urllib / requests`, `fetch(`, `/dev/tcp/` …) |
| `net.url-payload` (built in) | A web tool asked to open an address longer than 1024 characters |
| `net.local-url` (built in) | A web or browser tool asked to open an address on this machine or a private network, or one that is not `http(s)` (`javascript:`, `chrome:`, `ftp:` …) |
| `write.outside` (built in) | Writing a file outside the workspace (a shell redirect, `cp`, `write_file`, `patch`, a tool's output path) |
| `ask.explicit` (built in) | The agent asked: a command prefixed `OMNI_NEEDS=network ` or `OMNI_NEEDS=write ` |
| `opaque.sensitive` (built in) | A command the reader could not fully read that also names a sensitive place |
| `sandbox.unavailable` (built in) | Every shell command, on a machine where the sandbox does not work |
| `sys.services` | Starting, stopping or restarting a service (`launchctl`, `pm2`, `systemctl`, `kill`, `pkill`, `killall`, `shutdown`) |
| `sys.brew-services` | `brew services …` |
| `sys.agents` | Starting another agent from the shell (`hermes`, `claude`, `codex`, `gemini`, `aider` …) |
| `sys.automation` | Driving the Mac's apps (`osascript`, `shortcuts`, `automator`, `defaults`, `pmset`, `networksetup`, `diskutil`, clipboard, `tmux`, `screen`) |
| `sys.rm-recursive` | Recursive or forced delete outside the workspace |
| `sys.find-delete` | `find … -delete` |
| `sys.git-destructive` | `git reset --hard`, `clean -f`, `checkout -- .`, `branch -D`, `rebase`, `filter-branch`, `stash drop` |
| `sys.gh-notify` | Talking to people on GitHub: `gh issue|pr comment`, `gh pr review`, `gh api` writing to a `/comments`, `/reviews` or `/replies` endpoint. Outward: keeps its card under internal autonomy (`OUTWARD_APPROVALS`) |
| `sys.gh-write` | Changing GitHub (`gh pr / issue / release / repo create`, merge, close, edit; `gh api` with a body). Gets gh's credential; internal autonomy runs it without a card |
| `sys.calendar-write` | Changing the calendar without notifying anyone (`gog calendar create / add / update / edit / move / delete …` with no attendees; `focus-time` / `out-of-office` with `--auto-decline none`; `working-location`; `subscribe`; `create-calendar`), judged on gog's command words — argument text never lends another command this row's gog credential |
| `sys.gmail-housekeeping` | Tidying a Gmail mailbox Omni reaches (the agent's, the owner's personal): `gog gmail archive / trash / untrash / mark-read / unread`, `messages | thread | batch modify` (and `update / edit / set`), `labels list / get / create / rename / style / modify / delete`. Judged on command words; no Gmail settings, filters, forwarding, send-as, delegates, vacation, drafts or send word anywhere, no foreign credential. Gets gog's token store; internal autonomy runs it without a card (work unblock 2026-10-22). `read.gog-gmail` is now a read on command words too, so `gmail thread modify` is housekeeping, not a read |
| `sys.calendar-holds` | Hold proposed meeting times on the owner's calendar (`scripts/manage_holds.py place / release / confirm / attach-approval / reconcile / list / get`; the CLI counts slots from 1). A hold is an opaque event with no attendees, so it notifies nobody and the script has no flag that adds one. Gets gog's token store only (not the repo `.env`); `OMNI_BRIDGE_AUTONOMY=internal` runs it without a card. The invite to the other person is never this row: `gog calendar create --attendees` stays `send.calendar-invite` (blocked) and the invite is an `omni_propose` calendar-invite |
| `sys.google-write` | Change Google data (any gog command that is not a read): `gog … create / update / delete / share / upload`, and every other whole-command `gog` / `gws` that is not a read (`forms publish / add-question`, `docs add-tab / write / insert / find-replace`, `slides create` …), except when any word of it is Gmail / Chat / calendar, gog's own credentials or settings, a server (`mcp`, `serve`), or a send / reply / forward / draft / sendas / delegates / vacation word, or a flag before the group names another `--client` / `--home` / `--access-token` / `--config` (those keep only their own rows). The group is read from gog's command words, never from argument text. Gets gog's token store; `OMNI_BRIDGE_AUTONOMY=internal` runs it without a card |
| `sys.repo-scripts` | Any other script in the repo's `scripts/` (it acts with the agent's own credentials) |
| `sys.chmod-recursive` | `chmod -R`, `chown -R` |
| `sys.disk` | `dd`, `mkfs`, `newfs`, `asr` |

### 6. Hermes' own tools

| Row | Tool | Action |
|---|---|---|
| `tool.execute-code` | `execute_code` — runs in Hermes' own process, where no sandbox applies | approve, always (a script in the workspace run with the terminal needs no approval) |
| `tool.cron` | `cronjob` create / update / remove (list, status, verified run/resume and pause are free) | approve in guarded mode; internal mode frees local prompt jobs after the boundary checks below |
| `tool.skill-manage` | `skill_manage` | approve (can be freed) |
| `tool.memory` | `memory` writes (reads are free) | approve (can be freed) |
| `tool.browser-act` | `browser_type`, `browser_press`, `browser_cdp`, `browser_exec`, `browser_dialog`, and `browser_console` with JavaScript | approve (can be freed) |
| `tool.computer-use` | `computer_use` — the screen, keyboard and mouse | approve; refused off Omni turns |
| `meet.attend` | `omni_meet_attend` — Omni attends a meeting (listening only) and its address is added to the invite | approve; grantable; refused off Omni turns (`meet.door`) |
| `browser.signed-in` | Matches no tool by itself. An approved `action` names it in `grants` to let `browser_*` calls run on an item card's thread while the browser holds sign-ins (otherwise `net.card-thread`) | approve; grantable |
| `meet.speak` | `omni_meet_allow_speech` — Omni may answer aloud in that meeting when addressed by name | approve; grantable; refused off Omni turns (`meet.door`) |
| `meet.say` | `omni_meet_say` — this exact sentence, spoken now in the meeting Omni is in | approve; refused off Omni turns (`meet.door`) |
| `tool.home` | `ha_call_service` | approve |
| `tool.kanban` | kanban writes (they start other agent workers) | approve |
| `mcp.destructive` | An MCP tool that deletes, merges, prunes or changes many records, or manages tokens | approve |
| `mcp.untrusted-write` | A comment or reply (a name segment `comment` / `reply` / `mention`, not a read) through an MCP server that is not one of Omni's own stores (`OMNI_BRIDGE_MCP_SERVERS`, default `parachute,prism`): it notifies the item's collaborators. Every other connector write is internal work and runs (owner policy 2026-11-16, `policy.mcp_connector_write`): Canva create/edit/generate/resize/upload/export, ClickUp task edits. `remove_background` and `merge_designs` are design work, not deletes (`MCP_NOT_DESTRUCTIVE`). Hermes names MCP tools `mcp_<server>_<tool>`; the server is the longest configured `mcp_servers:` key that prefixes the name (`paths.mcp_server_names`) | approve; refused off Omni turns |
| `mcp.local-path` (built in) | An MCP tool given a local file by path outside the workspace. The server is another program, outside the sandbox, and would open it itself | approve. A file INSIDE the workspace runs (work unblock 2026-10-22): the server is handed a private copy read inside the sandbox, which nothing can change under it, removed when the call ends |
| `file.handover` (built in) | A tool that opens a local file itself, when the private copy cannot be made (the file is a secret by then, a folder, or over 64 MB) | refused |
| `tool.unknown` (built in) | A tool that is on no list — from a newer Hermes or another plugin | approve, until its name is added to `OMNI_BRIDGE_TOOLS_FREE` |

Free: `read_file` / `search_files` (not on a secret), `write_file` / `patch`
(inside the workspace), `web_search`, `web_extract`, `browser_navigate` /
`snapshot` / `click` / `scroll` / `back` / `vision` / `get_images`,
`vision_analyze`, `image_generate`, `text_to_speech`, `todo`,
`session_search`, `skills_list`, `skill_view`, `delegate_task`, `process`,
`clarify`, Home Assistant reads, kanban reads, vault reads and note writes,
Hermes' tool-search bridge (`tool_search`, `tool_describe`; `tool_call` is
judged as the tool it calls), and `omni_propose`. A file path or an address
handed to any of them is held to the rows above.

`omni_card` updates the card of the turn's thread through the loopback hook
(`POST /api/omni/hooks/cards/thread`). It sends nothing; drafts on it become
approvals only when the owner picks them. It is freely allowed for that reason
(an exact name match), and is offered on Omni turns only (one door offers
`omni_propose` on every surface; a card belongs to an Omni thread). Off Omni the
one door lets it pass: it is not an outward send.

**An item card's thread browses signed out (owner decision 2026-10-22).** A turn
in a thread that belongs to an item card (asked of the gateway,
`GET /api/omni/hooks/cards/thread/<id>`, cached 30 s) is driven by untrusted email
or chat text, and the option's plan was written from it. Proactive runs happen on
card threads too. Such a turn gets the same tools as any Omni turn, web, browser,
fetch and a networked contained shell included, because Hermes' browser runs in a
separate, signed-out profile (`browser.use_real_profile` false or absent in
`$HERMES_HOME/config.yaml`): nothing logged in is in reach. Also counted as signed
in: a non-blank `browser.cdp_url` or `BROWSER_CDP_URL` (attaches to an existing,
possibly signed-in Chrome), and camofox persistence or identity
(`browser.camofox.managed_persistence`, `.user_id`, `.session_key`). If any of
those is turned on, `policy.card_egress` blocks the BROWSER on card threads
(`browser_*`, also through `tool_call`; `net.card-thread`) until it is turned off,
unless an approved `action` running in that thread names `browser.signed-in` in
its grants. Since 2026-11-17 search, extract, fetch and a networked shell keep the
web there: they carry no sign-in, and a shell cannot reach the signed-in browser
(its DevTools port, `sandbox.browser_cdp_ports()`, default 9333 plus any port in
`browser.cdp_url` / `BROWSER_CDP_URL`, is denied in every sandbox tier; its
profile `~/Library/Application Support/Omni Chrome` is a secret directory;
`agent-browser --cdp` is `secret.browser-session`).

**Omni's own Chrome (2026-11-17).** Hermes' browser drives a real Google Chrome
(not headless Chrome for Testing, not a cloud browser on a datacenter address)
with its own profile, started by launchd `com.example.omni-chrome` on
`127.0.0.1:9333`, and `browser.cdp_url: http://127.0.0.1:9333`. Sites that flag
automation (Luma) see an ordinary Chrome on the agent host's own connection, and
sign-ins persist across turns. The owner's own Chrome profiles are untouched. `policy.real_profile_enabled` reads the
keys through Hermes (`hermes_cli.config.read_raw_config`), else the file with
PyYAML, and fails closed: a config it cannot read counts as signed in. A command
The owner approves card by card is still his call. The card lookup fails closed
too: no answer, or an answer that is not a plain yes/no, counts as a card thread.
The one door, the credential blocks, wallet writes and the guard's own files are
unchanged on card threads; the egress rule is the only thing that relaxed.

Accepted residual risk (The owner, 2026-10-22): with browsing on in threads driven
by untrusted email and chat text, a prompt-injected message could steer the agent
to visit a URL that leaks data in the request itself (a path or query string
carrying vault text). The signed-out profile keeps his accounts out of it; it does
not stop that. The mitigation is visibility: the host of every literal http(s)
URL in a card-thread call's arguments is logged at INFO as
`omni-bridge: card-thread egress <tool> <host>` (the host only, never the path or
query), so the weekly audit can list the domains. It records hosts a call was
about to reach, written before any later approval or sandbox decision, not proof
that the request went out. It does not see a `web_search` query or a URL
assembled in a shell variable. A hostname can itself carry data (a subdomain
built from vault text), and that host stays in the local `agent.log`.

---

## How an approval works

1. The plugin's `pre_tool_call` hook decides a call needs approval. It posts
   an approval of kind `command` to the Omni gateway
   (`POST /api/omni/hooks/propose`): the tool, the exact command (or the
   tool's arguments), the working directory, the rule, and why. The tool call
   is **paused** — the hook does not return.
2. The gateway stores it with a digest of exactly that content and sends an
   `approval` event on the thread's stream. The thread shows "needs you".
3. The app shows a card: **What** (the rule's title), **Why**, **Tool**,
   **In** (the directory), **Asked by** (shown when it is a sub-agent or a
   scheduled job rather than the conversation itself), and the whole command,
   unshortened. Anything else in the request is listed too, under its own
   name: nothing rides along unseen. Two buttons:
   **Approve Once** and **Deny**. A command card has no edit.
4. The app sends the decision with the digest of what it showed. The gateway
   refuses a decision whose digest is not the stored one.
5. The plugin polls (`GET /api/omni/hooks/approvals/:id`, every 1.5 s):
   - **approved** → the call runs, exactly as shown, in the `approved` tier;
     its output goes to the thread; the plugin reports how it ended and the
     card becomes *Ran* (`sent`) or *Failed*.
   - **denied** → it does not run; the model is told the owner denied it and
     not to try another way.
   - **no answer for ten minutes** (`OMNI_BRIDGE_APPROVAL_WAIT_S`) → the
     plugin withdraws the card; it does not run.
   - **the turn ends or is stopped first** → the gateway cancels the card
     (`turn-ended`); it does not run.
   - **the gateway is unreachable, refuses, or the call is too large to show
     (64 KB)** → it does not run.
6. Every propose, decision, withdrawal and result is in the gateway's
   approvals table and audit log, with the digest.

An approval is for one call. The same command a minute later asks again.
There is no "always allow" in the app; standing exceptions are settings on
the agent host (below), which the agent cannot write.

`OMNI_COMMAND_APPROVALS=off` in Prism's `.env` refuses every command card:
nothing that needs approval can run. It is independent of `OMNI_EXECUTORS`,
which governs sends.

### Approved actions — approve whenever, and it has what it needs (2026-11-05)

A command card waits ten minutes inside a running turn. Some things the owner asks for are
outward but have no draft kind of their own — submitting someone's booking page, a web form, a
sign-up — and he may want to approve them hours later. Before this, the agent had no way to ask
and either stopped or did something else instead (on 2026-11-05 it drafted an email invite in
place of the Cal.com booking he asked for).

1. The agent prepares the step (finds the slot, reads the form), then proposes kind `action`:
   `{title, reaches, url?, steps, grants?}`. `steps` holds every value it will enter. `grants`
   names the approval-tier rows the steps need (`tool.browser-act` to type into the page,
   `sys.browser` to start agent-browser, …). The plugin refuses any id that is not an
   approval-tier row (`guard_table.grantable()`; never a BLOCK row, never `tool.execute-code`)
   and adds `permissions`, the rows' titles, for the card. It stays open 7 days by default.
2. The card shows What, Reaches, Where, *May, without asking again*, and every step. Approve,
   Deny, or ask for a revision.
3. Approving starts a NEW turn in that thread with exactly the approved text. While that turn
   runs, Prism's `GET /api/omni/hooks/threads/<thread>/action-grants` names the grants and the
   plugin lets those rows through without a card (logged `granted by approved action apr_…`).
   Every other row behaves as always: blocks still block, other approvals still ask.
4. The turn's end closes the approval (`sent` if it finished, `failed` if not) and the grants
   are gone. A turn already running in the thread queues the action until it ends.

`OMNI_ACTION_APPROVALS=off` in Prism's `.env` refuses every action; `OMNI_EXECUTORS=off`
does too, because an action reaches other people.

---

## Every path into a tool

| Path | How the policy reaches it |
|---|---|
| A direct tool call | `pre_tool_call` |
| A file tool (`read_file`, `search_files`, `write_file`, `patch`) | The path rules, then the sandbox: its commands run on the turn's wrapped terminal environment. A `search_files` search (`rg` / `grep -r` / `find`) skips what the sandbox refuses: the "Operation not permitted" lines are dropped and an exit status 2 caused only by them becomes 0 (`contain.tolerant_search`). If that environment cannot be wrapped (a remote backend, a changed Hermes), each file-tool call asks instead |
| A tool that opens or writes a file in Hermes' own process | The path rules, then a private copy read inside the sandbox / a private destination delivered through it |
| Hermes' tool-search bridge (`tool_call`) | The hook sees the underlying tool; `policy.decide("tool_call", …)` also unwraps it |
| A sub-agent (`delegate_task`) | `subagent_start` records the child session under its Omni parent; every tool call of the child is judged the same way. A card says "Asked by: a sub-agent" |
| A cron job an Omni turn creates | Creating it needs approval. When it runs, its session id is `cron_<job>_…`; the plugin looks the job up, sees it came from the API server, and applies this policy. A card from it goes to the thread that created it; if that thread cannot be found the action does not run |
| `execute_code` | Always needs approval: it runs in Hermes' process |
| A background process (`terminal` with `background`) | The same command rewrite: it starts inside the sandbox and stays there |
| A script the agent wrote | Runs in the sandbox like any command |
| An MCP tool | Judged by server and verb; a path in its arguments by the secret rules |

---

## If the guard is not there

Hermes' API server offers a turn whatever `platform_toolsets.api_server`
lists. If that were Hermes' own full toolset and this plugin failed to load
— a bad merge, a typo, the plugin disabled — Omni would have a bare shell.
So the full tools are not listed by Hermes' name for them. The plugin
defines a toolset of its own, **`omni-full`** (the tools the Telegram agent
has, copied when Hermes starts), and the config lists
`[omni-full, omni]`:

- plugin loaded and working → `omni-full` exists → Omni has the full tools,
  under the policy;
- plugin missing, disabled, inert (no service token, a Buzz process) or
  crashed while loading → Hermes knows no toolset of that name → Omni is
  offered **no** terminal, file, code, browser or cron tool;
- plugin loaded but Hermes no longer consults it (a Hermes update that
  changed how hooks are called) → the plugin's **self-test** catches it: a
  few seconds after start, and again before the first Omni turn's first
  model call, it sends a secret read and a plain shell command through
  Hermes' own hook dispatch and checks that the first comes back refused
  and the second comes back in its sandboxed form. If not, it removes
  `omni-full` and logs `SELF-TEST FAILED … Omni's full tools are WITHDRAWN`.

A policy error on a single call blocks that call (fail closed, tested).

---

## Settings (Hermes' `.env` on the agent host — the agent cannot write it)

| Name | Default | Effect |
|---|---|---|
| `OMNI_BRIDGE_AUTONOMY` | guarded | `internal` removes routine internal-work prompts; sends, credentials and guard exclusions remain |
| `OMNI_BRIDGE_JOBS_API` | off | `1` enables the fingerprint-bound HTTP jobs adapter: normal claimed dispatch and loopback owner-thread routing; activate through runbook M5-J |
| `OMNI_BRIDGE_WORKSPACE` | `~/omni-workspace` (+ repo `outputs/`) | Where commands and file tools may write freely. Colon-separated |
| `OMNI_BRIDGE_SECRET_PATHS` | — | More directories or files that are secrets |
| `OMNI_BRIDGE_SECRET_FILES` | — | More env files whose VALUES are scrubbed from results (Prism's `.env`) |
| `OMNI_BRIDGE_SECRET_KEYS` | — | Names whose values are secrets even though neither the name nor the value says so. Comma-separated |
| `OMNI_BRIDGE_NOT_SECRET_KEYS` | — | Names whose values are NOT secrets although the name or the value looks like one |
| `OMNI_BRIDGE_GUARD_PATHS` | — | More places nothing may write, approved or not (the Prism checkout) |
| `OMNI_BRIDGE_EGRESS_ALLOW` | — | Hosts a plain `curl <url>` may fetch without asking |
| `OMNI_BRIDGE_LOOPBACK_PORTS` | — | Ports on this machine contained commands may reach (only ones that ask for a credential) |
| `OMNI_BRIDGE_MCP_SERVERS` | `parachute,prism` | MCP servers whose note writes are free |
| `OMNI_BRIDGE_FREE` | — | Rows marked "can be freed": `memory`, `skill-manage`, `browser-act` |
| `OMNI_BRIDGE_TOOLS_FREE` | — | Names of tools the guard does not know that may run freely |
| `OMNI_BRIDGE_ENV_KEEP` | — | More environment names passed into the sandbox (never credential-looking ones) |
| `OMNI_BRIDGE_MACH_ALLOW` | — | More system services for contained commands (never the Keychain, clipboard or app-launch ones) |
| `OMNI_BRIDGE_APPROVAL_WAIT_S` | `600` | How long a card waits |
| `OMNI_BRIDGE_SETTLE_S` | `8` | Seconds after Hermes starts before the plugin copies Telegram's tool list into `omni-full` and tests itself |

None of these can unlock a `block` row.

---

## How to add a row

1. Add it to `hermes/plugins/omni-bridge/guard_table.py`, in the list for its
   kind, with a new `id`.
2. Add a line to `CASES` (or `TOOL_CASES`) in `tests/test_omni_policy.py`: a
   command that must hit the row and one that must not.
3. Add it to the table on this page.
4. `venv/bin/python tests/test_omni_policy.py` — it fails if 2 or 3 is missing.
5. Merge, pull on the agent host, restart Hermes (the runbook's restart step).

Rows are matched block → reader → approve; inside a kind, first match wins.

---

## Threat model

**Who is the adversary.** The model itself, when something it read told it
what to do: an email, a web page, a note, a calendar description. Omni reads
a great deal of other people's text. Assume any of it can contain "ignore
your instructions and …". The policy does not try to detect that. It assumes
the model may at any moment do the worst thing its tools allow, and makes
that worst thing small.

**What a shell makes possible.** A shell on the agent host, unguarded, is
everything: read every token and key on the machine, send mail and money
with them, post them anywhere, install a program that does it again
tomorrow, rewrite the agent's own rules. That is why the shell is the one
tool that never runs outside the sandbox.

**What stops each of those, and where.**

| The worst case | Stopped by |
|---|---|
| Read a credential (any file, any encoding, any program) | The kernel: the files do not open inside the sandbox. File tools: path rules. Then redaction |
| Read a credential from the environment, another process, the Keychain, the clipboard | Clean environment; process info, Keychain and clipboard services denied in the sandbox |
| Send as the owner with the real senders | The table refuses them; without credentials and network they would fail anyway |
| Send some other way (raw SMTP, an API, a socket) | No network in the sandbox; with the network, by approval only, command shown |
| Carry data out over the network from the shell | No network; DNS denied too (a lookup can carry data) |
| Get out of the sandbox (a launchd job, an AppleEvent to Terminal, `open`, tmux, docker, a local web service, `crontab`) | Denied in the sandbox in every tier; local sockets and ports closed; setuid programs do not start |
| Persist (a LaunchAgent, a shell start-up file, a git hook, a cron line, a skill, a memory) | Guard paths are unwritable in every tier; OS schedulers refused; cron jobs by approval; skill and memory writes follow the explicit `FREE` setting |
| Switch the guard off (edit the plugin, Hermes' config, the toolset, the settings) | Guard paths; `hermes config` refused; `launchctl setenv` refused |
| Do it in a sub-agent or a scheduled job instead | Both inherit the policy |
| Do it with a tool nobody thought about | A tool on no list asks first |
| Swap a file for a link to a secret (or to the guard) between the check and the use | File tools run in the sandbox; in-process tools get a private copy made in the sandbox; writes are delivered through it |
| Use the guard's absence (plugin not loaded, or loaded and not consulted) | The full tools exist only as the plugin's own toolset, and only while its self-test passes |
| Delete or overwrite the owner's files | Outside the workspace: by approval. The guard's files and the secrets: never |
| Trick the owner into approving | The card shows the exact command and where; the digest binds the approval to it; no edit; one use |

**What this does not do — said plainly, with what covers it.**

- **The model reads what it reads.** Anything a free tool returns — a note, a
  mail search, a web page — is in the model's context, and so is sent to the
  model provider. That is what an assistant is. Credentials are kept out of
  it (layers 2 and 3). The owner's private documents are not.
- **Hermes' web tools are free, as the owner asked.** A web search or a page
  fetch sends its query or address to a third party. A model that has been
  turned can put a sentence of private text into one. Covered by: credential
  values are refused in any tool call; addresses over 1024 characters,
  local addresses and non-web schemes ask first; typing into a page asks
  first. A short private fact in a search query is not stopped. Stopping it
  means asking before every web request — a decision for the owner (see
  "Decisions").
- **Clicking in the browser is free; typing is not.** A click can press a
  button on a page where the browser is already signed in. Covered by: the
  agent cannot fill a form without approval, and `browser_cdp` /
  `browser_exec` / page JavaScript ask. If the agent host's browser is ever signed
  in to anything that matters, free `browser_click` should become a row.
- **An approved command is trusted for what it says.** `python3 deploy.py`,
  once approved, runs whatever is in `deploy.py`. It still cannot read a
  secret, touch the guard, or leave the sandbox — but it has the network and
  can write the owner's files. Read the card.
- **The sandbox is Apple's `sandbox-exec`.** Apple calls the command-line
  tool deprecated; it is the same kernel mechanism every sandboxed Mac app
  uses, and it is tested here on macOS 26. If an update breaks it, the
  plugin notices at start-up and every shell command asks instead.
- **The plugin runs inside Hermes.** A fault in the policy code blocks the
  call (fail closed, tested). A Hermes that stops consulting the plugin is
  caught by the self-test, which withdraws the tools ("If the guard is not
  there"). `tests/hermes_verify_omni_bridge.py` and the walk-through run
  against the real Hermes code to catch such a change before it reaches the
  agent host at all.
- **`execute_code` and approved in-process tools run in Hermes' own
  process**, where there is no sandbox: that is why they are approval-only,
  and why the card for `execute_code` shows the whole program.

---

## Evidence

- `tests/test_omni_policy.py` — every row; the shell reader; the real
  sandbox (secrets by eleven routes, writes, network, Keychain, launchd,
  AppleEvents, app launch, clipboard, local sockets).
- `tests/test_omni_bridge.py` — the hooks, approvals, scope (sub-agents,
  cron), fail-closed; file tools in the sandbox, private copies, the swap
  race, delivery of written files; `omni-full` and the self-test.
- `tests/hermes_verify_omni_bridge.py` — against real Hermes code: other
  surfaces see no change.
- `scripts/omni_policy_check.py` — for a person: what the policy would do
  with a command, run it the way Omni would, and `--selftest` the sandbox on
  this machine.
- Prism `apps/server/scripts/omni-walkthrough.ts` (REAL mode) — a real dev
  Hermes driven by a scripted adversarial model: the five usefulness checks
  and every attack above, end to end through the gateway.

`self.proton-owner-auth` blocks `proton_mail.py authorize-keychain` on every agent turn, including command approval requests. This explicit local owner GUI Terminal flow is not an agent capability. Normal health/search remain noninteractive.

### Scheduled outbound boundary

In both modes, `cron.delivery` lets a job deliver only to the owner himself (work unblock 2026-10-22): `deliver` is `local`, `origin` (only when the origin is his guarded Omni thread — also Hermes' default when `deliver` is omitted), `telegram` (his home channel) or `telegram:<OMNI_OWNER_TELEGRAM_CHAT>[:topic]` (`policy.owner_target`, the same rule `send_message` uses), or a comma list of those. `all`, another chat, another platform or anything else is refused on create, update, run and resume. Exact outbound actions must still use the proposal and approval executor when the scheduled agent runs. `cron.runner` rejects `script`, `monitor_script`, script-valued `monitor` and `no_agent` runners, which bypass that agent's guard. HTTP(S) URL monitors remain available as internal research; unknown monitor shapes fail closed. Ordinary local prompt jobs remain available in internal mode.

`cron.origin` requires creation from the actual `api_server` session context with a routable `omni_[0-9a-f]{8,64}` chat ID, matching Hermes' persisted origin. A nested cron/delegation context without that context must create the job from the Omni turn instead. Stored `api_server` origins let `_scope` guard future scheduled runs. Run, `run_now`, `trigger`, resume and update verify the stored job is local, has that origin and has no raw runner; `cron.job-unavailable` fails closed on missing IDs or failed lookup. List jobs and use the exact `job_id`, rather than a fuzzy name. Legacy unsafe jobs can be inspected, paused or removed, but must be replaced by a fresh local prompt job before execution. These checks cannot be bypassed by generic command approval or `tool_call`.

The trusted-owner arbitrary terminal/network caveat above still applies; this is the normal cron-tool boundary, not a claim to constrain every possible program the owner can run.


Matrix reads use `read.matrix`: a standalone pinned `matrix_reader.py` command
for rooms, recent (1–168 hours), read (1–500 messages, `--limit N` / `--limit=N` before or after the room), search, find-room, or `--help` / `-h` (bare or after one of those read verbs; work unblock 2026-10-22).
Only the existing repository environment is granted; no Docker or Keychain access
is granted. `read.matrix-arguments` refuses invalid or mutating CLI forms; health
remains an operator-only Docker probe. `send.matrix-reader` refuses direct send
and send-to, directing exact drafts to omni_propose kind message.

Authenticated readers must be separate terminal calls. Inline Python subprocess
aggregation, pipelines and compound commands do not inherit reader credentials.
An environment/state-file denial calls for the exact standalone reader, not an
OMNI_NEEDS network/write retry or a credential reset.

### Round 4 (review R4)
- `send.google-api` refuses google_api.py gmail send/reply/forward, every drafts … send, and
  calendar create/update/insert/patch/import/respond OUTRIGHT (argparse accepts option
  abbreviations such as `--attendee` / `--att`, so attendee matching is not attempted), plus
  drive share; subcommand prefixes are matched. `send.google-api-text` covers
  `python -m google_api …`, `import google_api` with its send functions, and a subcommand group
  named through a shell variable (`google_api.py $S send`).
- Inline code: `.drafts().send(` / `drafts.send` are sends.
- Prism control off Omni: `/auth/invite`, `/auth/invite-info`, `/auth/register` added; ANY host on
  port 8787 (the agent host's LAN name or IP, `--resolve x:8787:…`, leading zeros, hex `0x2253`) and a
  code socket to a loopback address on 8787 are refused — Prism owns 8787 on this machine.
- Residual (documented, not fixed): values computed at run time (`str(8700+87)`) and
  `--unix-socket`; for Prism the server-side rules (device-only approval, send grants) are
  the hard stop. (`events().insert(body=ev)` with attendees in a variable is now refused:
  every inline `events()` write is.) For Google there is no server-side stop — see round 5.

### Round 5 (review R5)
- **Top residual, stated plainly:** Google sends and calendar invites made with the Hermes
  Google token have NO server-side stop. Only the text guard stands between them and a send.
  The real fix is a read-only token scope, or running Hermes as a separate macOS user without
  the token (see "Top residual" near the top of this page).
- `secret.path` now covers every OAuth token / client-secret file: `google_token.json`,
  `google_client_secret.json`, `client_secret_<id>….json`, `*_token.json`, `token.json.bak`,
  `*.pickle` tokens (the word `token`/`client_secret` between `_ . -` separators; `tokenizer.json`
  is not one), and inside `~/.hermes` any file whose name carries `token`, `client_secret` or
  `credential(s)` as a word. Same normalisation and glob handling as the other secret names
  (`cat ~/.hermes/*tok*`, `…/google_tok?n.json`); code that names the token, `client_secret`, or
  calls `Credentials.from_authorized_user_file|info` is refused.
- `send.google-rest` (off Omni, shell and code text, after URL normalisation): Gmail
  `…/messages/send`, `…/drafts/send`, `…/messages/import`, `…/messages/insert` on
  `gmail.googleapis.com` or `www.googleapis.com/gmail/…`, the bare `gmail/v1/users/…/(messages|
  drafts)/(send|import|insert)` path, `googleapis.com/upload/gmail` and `…/batch/gmail`; a Calendar
  `calendar/v3/calendars/…/events` URL with `sendUpdates=all|externalOnly` (or
  `sendNotifications=true`) or any write marker (`POST/PATCH/PUT/DELETE`, curl data flags,
  `data=`/`json=`, `urlopen`, `request(`, `fetch(`); the same for Drive `files/…/permissions`.
- `send.google-api-copy`: `cp|ln|mv|rsync|install|ditto|tee|dd …` naming google_api.py, or
  `cat|sed|head … google_api.py > file`, is refused off Omni. `send.google-api` (above) refuses the
  google_api.py send shapes for any interpreter-run script, so a copy made another way is
  caught when it is run with `gmail send`, `calendar create`, `drive share` ….
  `send.google-api-code`: inline code naming `google_api` at all (import, runpy, exec, copy).
- **Known limit, cannot be caught by text matching:** `getattr(obj, 'se'+'nd')(…)` (or
  `'ins'+'ert'`) on a Google service object built some way the guard did not see — e.g. from
  credentials obtained without naming the token file. The fix for that is the token scope above.
- `_PRISM_PORT` (the any-`:8787` check) is linear time: a literal `:`, at most 16 zeros, the port;
  no `:` in a repeated class, no nested or unbounded quantifier. 200k characters of `a:`/`[a]`/
  `a.` take a few hundredths of a second (test: `ReviewRound5.test_prism_port_is_linear`).

### Round 6 (review R6)
- **I-d** `send.gws-bridge`: off Omni, `gws_bridge.py` is treated exactly like `gws` — only
  gws's read forms run (`_gog_read_only` on its arguments), so `gmail +send`, `gmail users
  messages send`, `calendar +insert` / `events insert` (with attendees or without) and `drive
  permissions create` are refused. It is named in the copy rule (`send.google-api-copy`,
  which now also catches `tee … < sender`) and the inline-code rule (`send.google-api-code`:
  code naming `google_api` or `gws_bridge`). gws's send shapes are refused for any
  interpreter-run script, so a renamed copy is caught when it sends.
- **M-h** The token-name pattern is linear: in Python every `[^/]*` name part of
  `SECRET_NAME_PATTERNS` is capped at `[^/]{0,255}` (no nested quantifiers); the code-text
  token-file check finds each `[\w.-]+\.json` word once and tests its stem. The sandbox
  profile keeps the unbounded form, because macOS SBPL regex silently never matches `{m,n}`
  (checked with `sandbox-exec`) and the kernel matcher does not backtrack. Also capped:
  `prism-server[^…]{0,255}\.db` in code text, which took 28 s on 200k characters of
  `prism-server`. Test: `ReviewRound6.test_token_name_pattern_is_linear` (CPU time
  < 0.5 s per call on 200k characters of `_token_` and similar).
- **M-g** (documentation only): the remaining text-guard evasions are listed under "Top
  residual" near the top of this page. The Google token must be issued read-only before it is
  ever signed in on the agent host, or Hermes must run as a separate macOS user.



## Python start-up hooks in credentialed runs (closed 2026-10-23)

The gap: a credentialed Python (a reader or approved script holding gog's token or the repo `.env`) runs `sitecustomize`, `usercustomize` and every `.pth` line in its `sys.path` before the script's first line. With internal autonomy an Omni turn could write one into a user site directory or a trusted interpreter's `site-packages` and the next credentialed run would execute it, past the guard (it could call `gog calendar create --attendees` from inside the process).

Closed two ways:
- **No inherited start-up variables, no user site.** A command with a credential (any decision with secrets, keychain or trusted env) runs with `PYTHONNOUSERSITE=1`; no `PYTHON*` name passes through the environment even if listed in `OMNI_BRIDGE_ENV_KEEP`, and `PYTHONPATH`, `PYTHONSTARTUP` and `PYTHONHOME` are dropped from any extra env. (The wrapper already ran `env -i` with an allowlist; this makes the credentialed case explicit.) `sandbox.wrap`.
- **Site directories are guard paths** (`paths.python_site_dirs()`, in `guard_paths()` and the outward guard): `~/Library/Python` and `~/.local/lib` (kept whole so a missing user site cannot be created), and `site-packages` under the prefix of every trusted interpreter or program found through `trusted_binaries()` (Homebrew, Xcode, python.org, uv, the repo venv, Hermes' venv). A write there is `self.files` BLOCK, approved or not.

Not affected: a virtualenv under the workspace is not a trusted interpreter, so `pip install` there still works. `pip install --user` and `pip install` into a trusted interpreter's site are now blocked; use a workspace venv. Residual: a hook placed by anything that is not an Omni turn (a process running as the user) is outside the guard, as stated for the one door generally.

Fix round 2 (same gap, wider): a Python also runs its OWN standard library before the script (`sitecustomize.py`, `encodings/`, forged `__pycache__/*.pyc`), so `site-packages` alone is not enough, and a venv runs its BASE interpreter's stdlib. `paths.python_prefixes()` guards whole: the real prefix of every trusted interpreter, the base prefix of every venv among them (`home` in `pyvenv.cfg`: the repo venv and Hermes' venv) together with the symlink name `home` uses (uv's `cpython-3.11-…` → `cpython-3.11.15-…`), `~/.local/share/uv/python`, and Homebrew's `python@*` Cellar directories. Each is also an anchor root, so its parents cannot be renamed or swapped. A prefix that is root-owned all the way up (the Command Line Tools) is already safe and skipped; shared prefixes (`/usr/local`, `/opt/homebrew`, `~`) are never guarded whole. A workspace venv stays writable and its base stays readable. Approved re-wraps (`contain.py`) also carry `PYTHONNOUSERSITE=1`. Consequence: from an Omni turn, `uv python install`, `brew upgrade python@*` and a `uv run` that must download a Python fail in the sandbox (the install trees are guarded); the owner runs those himself. A workspace venv's `pip install` is unaffected.
