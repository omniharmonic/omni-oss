---
name: omni-tasks
description: Read, add, complete or drop the owner's tasks and his waiting-on list. Use when the owner asks what's on his plate, what's overdue, what others owe him, or to add/finish/drop a task.
version: 1.1.0
metadata:
  hermes:
    tags: [omni, tasks, parachute]
    category: productivity
---

# Omni tasks

Parachute (`tag:task`) is the ONLY task store. Never write tasks anywhere
else, never create task notes directly with the vault MCP — go through the
CLI so the quality bar applies.

Run from the Omni repo (the agent directory of this repository) with
`venv/bin/python scripts/omni_cli.py`:

| Need | Command |
|---|---|
| Active list | `tasks` (or `tasks list`) |
| Overdue (real deadlines only) | `tasks list overdue` |
| What others owe him | `tasks list waiting` |
| Machine-readable | add `--json` to any list |
| Add | `task-add <imperative title>` |
| He finished one | `tasks done <parachute-id> --commit` |
| He wants one dropped | `tasks drop <parachute-id> --commit` |

The id is printed in parentheses by `tasks`. Without `--commit`, `done` and
`drop` only print what they would change — run that first if you are unsure
which task he means, then repeat with `--commit` once he has confirmed. Never
complete or drop a task he did not ask about, and never write task notes
through the vault MCP or a `python -c` one-liner.

Rules the code enforces, so explain them rather than work around them:
- A task is something THE OWNER will do, phrased as an action. "Kevin forked
  the repo" is rejected — it's a report, not a task.
- Someone else's commitment is status `waiting`, not a task on his list.
- A deadline guessed from a meeting ("next week") is shown with `?` and is
  never overdue. Only a stated date is.
- ClickUp mirrors (`vault/tasks/clickup/…`) sync back to a shared
  ClickUp workspace — changing their status changes ClickUp. Say so before doing it.
