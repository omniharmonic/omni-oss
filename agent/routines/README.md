# Routines

Omni has two kinds of scheduled work. Both only prepare: nothing leaves on the owner's behalf
without an approval in the Omni app.

## 1. Engine routines (run by Hermes, on the agent host)

`scripts/proactive_intelligence` is the routine runner. One process reads the vault, asks one
bounded model call, validates the answer and publishes it to the Omni app through the Prism
server (`POST /api/omni/hooks/routine` and the card hooks):

| Job | What it does | Default schedule (owner's timezone) |
|---|---|---|
| hourly pass (no `--routine`) | New mail, chats, meetings and tasks become approval cards | :05 past each hour, 08–20 |
| `--routine events` | The same pass, woken by a content-free event signal | every minute, 08–20 |
| `--routine morning` | One morning briefing in its own Omni chat | 07:15 |
| `--routine evening` | One evening review | 19:00 |
| `--routine tweet` | One short-post draft in the owner's voice, from their own writing notes; approval only | 10:00 |
| `--routine nightly-review` | Commitments, decisions and connections from recent transcripts (proposals only) | 05:30 |
| `--memory-sync` | Mirrors Hermes' memory files into the vault (`scripts/hermes_memory_sync.py`) | :17 past each hour |
| `--health` | Daily check that the jobs produced output (`scripts/omni_health.py`) | 21:00 |

Install them one at a time with `scripts/install_proactive.py`. It is a dry run unless `--apply`,
a routine is installed paused unless `--enable`, and every apply writes a receipt directory that
`--rollback` can undo:

```sh
cd agent
python3 scripts/install_proactive.py --routine-recipes                 # print every routine job
python3 scripts/install_proactive.py --routine morning                 # dry run: the plan
python3 scripts/install_proactive.py --routine morning --apply --receipt state/receipts/morning-1
python3 scripts/install_proactive.py --routine morning --enable --apply --receipt state/receipts/morning-2
python3 scripts/install_proactive.py --memory-sync --apply --receipt state/receipts/memory-sync-1
```

The jobs use Hermes' own cron API and run `python -m proactive_intelligence --routine <name>`.
Times are in Hermes' timezone, which `hermes/install.py` sets from `owner.timezone` in
`config/identity.yaml`; the scripts read the same zone from `OMNI_TIMEZONE`.

## 2. Prompt routines (this folder)

Each `routines/<name>/SKILL.md` is a portable skill (front matter + prompt) for an agent that has
the vault's tools: a desktop or cloud scheduled task, or a Hermes skill. They are **templates**:

| Template | Writes |
|---|---|
| `morning-briefing` | `vault/agent/insights/{date}` |
| `midday-sensing-scan`, `afternoon-sensing-scan` | `vault/agent/sense/{date}-midday` / `-afternoon` |
| `evening-review` | `vault/agent/deliberations/{date}-evening` |
| `meeting-prep` | `vault/agent/meeting-prep/{date}-{title}` |
| `process-vault-transcripts` | one canonical meeting note per transcript, plus a run report |

Placeholders (`{{owner.name}}`, `{{owner.call_me}}`, `{{owner.email}}`, `{{owner.timezone}}`,
`{{agent.name}}`) are filled from `config/identity.yaml` by `scripts/sync_routines.py`, which also
copies them to the desktop scheduler folder (`--to-desktop`) and prints cloud create/update
bodies. `metadata.omni.routine_id` is `none` until you create the routine somewhere and record
its id (`--record-create`).

Every template carries the generated "Field shapes" block (`scripts/gen_field_shapes.py`, from
`config/vault-shapes.json`); `python3 scripts/gen_field_shapes.py --check` fails when one is stale.

Left out on purpose: the original install's daily-tweet prompt and its nightly vault-weave prompt,
which were written around one person's voice, vaults and organizations.
