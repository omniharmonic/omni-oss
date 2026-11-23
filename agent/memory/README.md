# Memory

This folder ships empty. Your agent's memory is yours; none of the original install's memory,
state or outputs is in this repository, and `.gitignore` keeps yours out of git.

Omni remembers in three places, all of them local to your install:

| Store | What it holds | Code |
|---|---|---|
| Hermes memory: `~/.hermes/memories/MEMORY.md` and `USER.md` | Durable facts Omni writes with Hermes' `memory` tool (people's time zones, preferences, how a scheduling round went). First-class and writable every turn. | Hermes; limits raised by `hermes/install.py --memory-only` (8000 / 4000 characters) |
| The vault (Parachute) | Everything the routines and cards read and write: people, projects, meetings, tasks, briefings. A read-only **mirror** of Hermes memory lands at `vault/agent/memory/hermes-memory` and `hermes-user` (tag `agent-memory`). | `scripts/parachute_client.py`, `scripts/parachute_writer.py` (shape-guarded writes), `scripts/hermes_memory_sync.py` (the mirror, one direction, secret values scrubbed first) |
| `state/` (git-ignored) | Run state: the proactive engine's SQLite store, the mirror's hashes, the learning audit trail, scheduling holds | `scripts/proactive_intelligence/state.py`, `scripts/proactive_intelligence/learning_audit.py`, `scripts/manage_holds.py` |

## The schema

- `config/vault-shapes.json`: the field-shape contract (which fields are lists, the allowed task
  statuses and labels). `scripts/vault_shapes.py` enforces it on every write; the routine prompts
  carry it as a generated block (`scripts/gen_field_shapes.py`).
- `docs/tag-schemas.md`: the vault's tag schemas (person, project, meeting, task, …);
  `scripts/apply_tag_schemas.py` pushes them to a vault.
- Note vocabulary used by the code: commitments carry `direction` `owner_to_other` or
  `other_to_owner`; a task's `deadline_source` is `stated`, `owner`, `guessed` or `none`.

## Learning, audited

Proactive runs may add to Hermes memory and skills. Every such write, and every refused one, is a
line in `state/learning-audit.jsonl` (intent before, result after), so you can see and revert what
the agent learned on its own.

## Turning the mirror on

```sh
cd agent
python3 scripts/hermes_memory_sync.py --dry-run          # what would be written
python3 scripts/install_proactive.py --memory-sync --apply --receipt state/receipts/memory-sync-1   # hourly
```
