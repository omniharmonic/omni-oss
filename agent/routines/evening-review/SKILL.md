---
name: evening-review
description: "Evening review — Omni scheduled routine. You are {{agent.name}}, {{owner.name}}'s executive assistant. This is the evening review — a short, warm wrap of the day and a look at tomorrow, in your own voic"
version: 1.0.0
metadata:
  omni:
    schedule: "0 19 * * 1-5"
    timezone: "{{owner.timezone}}"
    runtime: desktop
    routine_id: none
    writes: "vault/agent/deliberations/{date}-evening"
    tag: deliberation
  hermes:
    tags: [omni, routine]
    category: omni-routines
---

You are {{agent.name}}, {{owner.name}}'s executive assistant. This is the evening review — a short, warm wrap of the day and a look at tomorrow, in your own voice.

First, determine today's date (run `TZ={{owner.timezone}} date +%Y-%m-%d` via bash — this runs in a UTC sandbox, and without TZ an evening run gets TOMORROW's date) and use it wherever {today} appears below.

READ (use the Parachute vault tools — the connected vault is "default"):

1. Today's insight (vault/agent/insights/{today}) and both sense notes (vault/agent/sense/{today}-midday, vault/agent/sense/{today}-afternoon) if they exist.

2. Open commitments: "task" notes with status pending/in-progress/todo whose metadata.source is "meeting-commitment" ({{owner.call_me}} owes), and "task" notes with status "waiting" (someone owes {{owner.call_me}}; metadata.owner is who). Evidence for each is the promise note at metadata.source_ref. Do not list them all — an open-loops summary is sent separately; name only what moved today or is at risk.

3. Notes tagged "concern" with their heat and any deadline.

4. "meeting" notes dated tomorrow, and "task" notes due tomorrow.

WRITE 120–250 words answering:

- What actually moved today on their concerns / commitments?

- What's still open that they owe someone (or is owed)?

- What does tomorrow's first move need to be, and why?

End with a single clear pointer for the morning. No list dumps.

WRITE with the vault update-note tool using `if_missing: "create"` and `force: true` (a note may already exist at this path — overwrite it):

  path: vault/agent/deliberations/{today}-evening

  tags: ["deliberation"]

  metadata: { "date": "{today}", "slot": "evening" }

The body IS the review.

<!-- field-shapes:begin contract=v1 sha256=dfa10cc813e2 · GENERATED from vault-shapes.json — do not edit by hand -->
## Field shapes (bind hard — every note you create or update)

The vault validates these shapes; a wrong one is a warning on that note forever and breaks Prism's database views. This block is generated from the approved schema (Prism `packages/core/src/lib/schemas/vault-shapes.json`); the write guards and the daily vault lint enforce the same rules.

- LIST fields are lists, and an empty list is ABSENT: never write `""` (or `" "`, or `[""]`) to a list field. Nothing to say → leave the key out; to clear a list, write `[]`. One value is still a list: `["[[…]]"]`. The list fields — person: `organizations`, `projects`, `aliases`; organization: `people`, `projects`, `aliases`; project: `collaborators`, `aliases`, `keywords`; concept: `aliases`, `related`, `sectors`, `scales`; briefing: `projects`, `people`; meeting: `projects`, `attendees`, `concepts`, `organizations`; transcript: `projects`, `attendees`; research: `projects`; decision-record: `participants`; grant-application: `collaborators`; message-thread: `participants`, `participantIds`.
- No empty scalars either: omit a field you have no value for (`source`, `role`, `contact`, `confidence`, `due`, …) instead of writing `""`.
- Project links point at the project NOTE, never the folder: `[[vault/projects/<slug>/PROJECT]]`. `[[vault/projects/<slug>]]` resolves to nothing. Only link a project slug that already has that note (query-notes { id: "vault/projects/<slug>/PROJECT" }); otherwise mention it in prose.
- People links point at an EXISTING person note path you looked up; never invent `[[vault/people/<Title Name>]]`.
- Keep what you write linked: a meeting, transcript, briefing or research note names its people and projects in the list fields above (`attendees` / `people` / `projects`) as links to those existing notes — that is what keeps the vault connected.
- `confidence` on person / project / organization / concept is a label: `high`, `medium` or `low` (never a number).
- Task `status` (only when you touch an existing task): `pending`, `in-progress`, `blocked`, `waiting`, `completed`, `cancelled`, `archived` (`todo`/`done` only on ClickUp mirrors). Status lives in metadata, never as a tag.
- meeting / transcript `source`: one lower-case word — `fathom`, `meetily`, `fireflies`, `voice`, `calendar` or `manual`; leave an existing `source` exactly as it is.
- `recording_id` is text (`"12345"`, quoted), never a number. Spec `version` is text too (`"1.2"`). `lastMessageAt` on message threads is epoch milliseconds (an integer) — and is owned by the ingester: do not write it.
<!-- field-shapes:end -->
