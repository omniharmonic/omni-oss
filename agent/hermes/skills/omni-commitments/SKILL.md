---
name: omni-commitments
description: The owner's commitments ledger — what he promised in meetings, what others promised him, and proposals awaiting his yes/no. Use for "what did I commit to", "what are people waiting on from me", or when he says drop/keep/done about a captured commitment.
version: 1.0.0
metadata:
  hermes:
    tags: [omni, commitments, promises, parachute]
    category: productivity
---

# Omni commitments ledger

Meeting transcripts are mined for commitments by `scripts/ledger.py`
(via `omni_cli.py extract`). Each becomes a `promise` note (the evidence)
and, if clear, a task:

- The owner's clear commitment  -> task on his list  (promise `tracked`)
- Someone's commitment to him  -> `waiting` task     (promise `tracked`)
- Between other people         -> `fyi`, never surfaced
- Ambiguous                    -> `proposed`, shown at most twice, then `stale`

Commands (`venv/bin/python scripts/omni_cli.py …` in the Omni repo):

| Need | Command |
|---|---|
| What's open / needs a decision | `open-loops` |
| He says "yes, that's mine" | `loop <promise-id> keep` |
| He says "not mine" / "drop it" | `loop <promise-id> drop` |
| He says it's done | `loop <promise-id> done` |

Never create or edit `promise` notes directly, and never tag a promise
`task` — routing lives in ledger.py so it happens exactly once.
