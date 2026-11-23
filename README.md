# Omni (open-source variant)

Omni is a personal agent with a native app. It drafts, prepares and remembers for one person (the owner), and nothing leaves on the owner's behalf unless they approve that exact draft in the app.

This repository is the public, config-driven variant of a private, single-person install. Everything that named that person (identity, accounts, chat ids, wallets, organizations, paths, routines, memory and outputs) has been removed or moved to an example config. It holds the app, the agent's guarded bridge, the routine runner and routine templates, the integration adapters (mail, calendar, chat, vault), the Google Meet bot and the memory system's code and schema. Some parts of the original are still not here; see [What is not here yet](#what-is-not-here-yet).

## How it fits together

```
Omni app (app/Omni, macOS + iOS)
   │  device sign-in, /api/omni/*  (SSE for replies, approval cards, push)
   ▼
Prism Server with OMNI_ENABLED=true      ← github.com/omniharmonic/prism (apps/server/src/omni)
   │  OMNI_SERVICE_TOKEN, Hermes' API server
   ▼
Hermes (NousResearch hermes-agent) + this repo's plugins (agent/hermes/plugins)
   omni-bridge          guarded tools: contained shell, drafts via omni_propose, approval cards
   omni-resource-guard  caps browser sessions and fan-out under memory pressure
   omni-core-patches    small fixes to Hermes core the bridge relies on
   omni-meet            attends Google Meet meetings the owner approves; speaks only where approved
   hermes/skills        briefing, tasks, commitments, scheduling, meetings, proactive procedures
   │
   ├─ scripts/proactive_intelligence   the routine runner: hourly cards, morning / evening briefings
   ├─ scripts/*                        adapters: Proton Mail Bridge, Google (gog), Telegram, Matrix,
   │                                   Parachute vault, tasks, calendar holds, one-door send grants
   └─ routines/                        prompt routines as templates filled from config/identity.yaml
```

Every outbound path is behind one door: a script that sends (mail, chat, calendar invite) refuses to run without a grant the Prism server signs when the owner approves that exact draft in the app (`scripts/send_grant.py`). Without the grant, Omni can only propose (`scripts/omni_propose.py`).

## Requirements

- A [Prism](https://github.com/omniharmonic/prism) server (its `docs/onboarding.md`), with the Omni module turned on (`OMNI_ENABLED=true`, see Prism's `docs/omni-module.md`).
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed for the same user (default home `~/.hermes`), with its API server platform enabled.
- Python 3.9+ with PyYAML (`pip install pyyaml pytest`) for the install script and tests.
- For the app: macOS 26 with Xcode 26 (Swift 6.2), and a Prism checkout for `swift/PrismKit`.

## Set up the agent

```sh
cd agent
cp config/identity.example.yaml config/identity.yaml    # who the agent is and whom it works for
$EDITOR config/identity.yaml

python3 hermes/install.py                 # dry run: shows every change
python3 hermes/install.py --apply         # link + enable the plugins, render SOUL.md, set limits
```

Then put the connection settings in Hermes' environment (`~/.hermes/.env`; never commit them):

| Variable | What |
|---|---|
| `OMNI_SERVICE_TOKEN` | The service token your Prism server's Omni module gives Hermes (shared secret). Without it omni-bridge stays inert. |
| `OMNI_GATEWAY_URL` | Your Prism server's Omni gateway, e.g. `http://127.0.0.1:8787` when both run on one machine. |
| `OMNI_OWNER_TELEGRAM_CHAT` | Optional. The owner's own Telegram chat id: the only Telegram target Hermes may message directly. Empty = none. |
| `OMNI_BRIDGE_WORKSPACE` | Optional. Where Omni turns may write (default: a workspace under the Hermes home). |

The routines, integrations and the Meet bot read who the owner is from the environment as well (put these in `~/.hermes/.env` and in `agent/.env`; both are git-ignored). Each one fails closed when empty: no account means no mail, calendar or Meet call is made.

| Variable | What |
|---|---|
| `OMNI_OWNER_NAME` | The owner's name, e.g. `Alex Example`. Used in meeting titles, prompts, and to recognise the owner's captions in a meeting. |
| `OMNI_OWNER_EMAIL` | The owner's primary (Google calendar) address. Calendar reads, holds and Meet invites use only this account. |
| `OMNI_OWNER_EMAILS` | Optional. The owner's other addresses, comma-separated, so their own mail is never treated as inbound. |
| `OMNI_AGENT_EMAIL` | Omni's own Google account, the one that joins meetings. |
| `OMNI_TIMEZONE` | The owner's IANA timezone (same as `owner.timezone`). Defaults to UTC. |
| `PROTON_BRIDGE_USER` | Optional. The Proton Mail Bridge mailbox, if it differs from `OMNI_OWNER_EMAIL`. Its password lives in the macOS Keychain (`scripts/secrets.sh`), never in a file. |
| `PARACHUTE_URL`, `PARACHUTE_VAULT`, `PARACHUTE_API_KEY` | The vault the routines read and write (default `http://127.0.0.1:1940`, vault `default`) and its token. |
| `TELEGRAM_BOT_TOKEN` | Optional. With `OMNI_OWNER_TELEGRAM_CHAT`, lets `scripts/send_telegram.py` notify the owner's own chat (nobody else). |
| `MATRIX_HOMESERVER`, `MATRIX_USER`, `MATRIX_ACCESS_TOKEN` | Optional. The Matrix bridge reader (`scripts/matrix_reader.py`). |

Omni's commands run with an empty environment except an allow-list (`hermes/plugins/omni-bridge/sandbox.py`, `ENV_ALLOW`), which includes the identity names above but never a credential.

Routines are installed one at a time, paused first: see [`agent/routines/README.md`](agent/routines/README.md). The memory system is described in [`agent/memory/README.md`](agent/memory/README.md). The Meet bot needs Hermes' `google_meet` plugin as a library, Playwright Chrome, and (for speaking) the BlackHole virtual microphone; see [`agent/hermes/skills/omni-meetings/SKILL.md`](agent/hermes/skills/omni-meetings/SKILL.md).

In Hermes' `config.yaml`, give the API server the Omni toolsets: `platform_toolsets.api_server: [omni-full, omni]`. Restart Hermes; its gateway log shows `omni-bridge: ACTIVE` once the self-test passes.

The guard's rules are one table, `agent/hermes/plugins/omni-bridge/guard_table.py`, documented in [`agent/docs/omni/tool-policy.md`](agent/docs/omni/tool-policy.md) (written for the original install; it still mentions its scripts and accounts as examples).

Tests: `cd agent && python3 -m pip install pyyaml pytest python-dotenv requests && python3 -m pytest -q tests` (the bridge and policy suites take a few minutes). `tests/conftest.py` sets a fictional owner; it also lists, by name, the few tests that need scripts this repository does not ship. Set `PRISM_REPO` to a Prism checkout to also check the vendored vault contract against it.

## Build the app

The app depends on PrismKit, which lives in the Prism repository (`swift/PrismKit`). Clone Prism next to this repository, or set `PRISMKIT_PATH`:

```sh
git clone https://github.com/omniharmonic/prism.git
git clone https://github.com/omniharmonic/omni-oss.git
cd omni-oss/app/Omni
swift build && swift test            # or: PRISMKIT_PATH=/path/to/prism/swift/PrismKit swift test
open Omni.xcodeproj                  # scheme Omni
```

Before signing, set your own `DEVELOPMENT_TEAM` and replace the `com.example.omni` bundle ids in `Omni.xcodeproj` (and `Support/ExportOptions.plist`). For push, your Prism server's `OMNI_APNS_TOPIC` must equal your bundle id, with your own APNs key. More in [`app/Omni/README.md`](app/Omni/README.md) and [`app/Omni/TESTING.md`](app/Omni/TESTING.md).

## What is not here yet

The original agent is a much larger system. Not included, by design or for a later pass:

- **Never published: data.** Memory, state, outputs, logs, transcripts, vault exports, contact lists, the original persona, `.env` files, keys, tokens, OAuth client files and wallet material. `agent/memory/` ships empty and `.gitignore` keeps yours out.
- **Owner-specific routines**: the original's daily-tweet prompt and nightly vault-weave prompt (written around one person's voice and vaults). The engine's `tweet` routine is here; its prompt is generic.
- **The Telegram bot and its relays** (`telegram_bot.py`, `relays.py`, the `omni_cli.py` command hub and `hermes/scripts/omni-run.sh`), the **missions** engine, the rest of the **standing agent** (bootstrap, dedup, deliberation, writers), the **dashboard**, **swarm** and **Buzz** chat agents, the tweet senders (`omni_tweet_send.py`, `twitter_post.py`), `meeting_followup.py`, the wallet and Safe scripts, `omni_ship.py` (self-improvement shipping) and the launchd job files. The guard table still names some of these as things it blocks or gates, and the briefing, tasks and commitments skills still point at `omni_cli.py` for their read commands; the scripts themselves are not shipped. `tests/conftest.py` lists the six tests skipped for that reason.
- The Prism-side dev stub for running the app without a real Hermes (`apps/server/scripts/omni-dev.sh`) is not in the public Prism repository yet.

## Licence

GNU Affero General Public License, version 3 (AGPL-3.0-only). See [`LICENSE`](LICENSE).

Copyright (C) 2026 the Omni contributors.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

Third-party: the app's speech pipeline uses [FluidAudio](https://github.com/FluidInference/FluidAudio) (Apache-2.0).
