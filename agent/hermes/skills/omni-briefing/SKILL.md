---
name: omni-briefing
description: Pull the owner's current briefing, sensing scan, evening review, urgent-mail alerts or calendar on demand. Use when he asks for his briefing, "what's going on today", what's urgent, what's on his calendar, or the latest sense note.
version: 1.1.0
metadata:
  hermes:
    tags: [omni, briefing, parachute]
    category: productivity
---

# Omni briefing (on demand)

Scheduled thinking is done by Claude cloud routines that write notes to
Parachute; Omni only RELAYS them. Do not compose a new briefing from raw
email/calendar — relay what the routines wrote.

`venv/bin/python scripts/omni_cli.py <cmd>` in the Omni repo, WITHOUT
`--commit` (on demand never marks anything relayed):

| Need | Command |
|---|---|
| Morning brief (lead + intelligence + open loops) | `morning` |
| Sensing scan | `sense midday` / `sense afternoon` |
| Evening review | `evening` |
| What's urgent / needs a reply (new since the last alert) | `alerts` |
| Today's calendar / the week ahead | `calendar today` / `calendar week` (`--json` for data) |

`morning` and `evening` also run the EA (standing agent) pass and print any
action it would surface; that costs two model calls, so do not run them just
to answer a calendar or task question — use `calendar` / the tasks skill.

Empty output means there is nothing new — say so in one line.
Privacy: calendar titles (including `calendar` output), email subjects and people in these notes are for
The owner only. Never repeat them in any shared channel (Buzz, group chats).
