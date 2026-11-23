---
name: process-vault-transcripts
description: "Process vault transcripts — Omni scheduled routine. Process all unprocessed meeting transcripts in the local Parachute vault, end to end, using ONLY the parachute-vault MCP tools (query-notes, update-no"
version: 1.0.0
metadata:
  omni:
    schedule: "0 18 * * *"
    timezone: "{{owner.timezone}}"
    runtime: desktop
    routine_id: none
    writes: "vault/agent/reports/{date}-transcript-processing"
    tag: report
  hermes:
    tags: [omni, routine]
    category: omni-routines
---

Process all unprocessed meeting transcripts in the local Parachute vault, end to end, using ONLY the parachute-vault MCP tools (query-notes, update-note, create-note, find-path, delete-note) plus the Read and Grep file tools. Do NOT use bash, curl, a REST API, localhost, or any token file — this task runs in an isolated sandbox that cannot reach the user's Mac (no localhost:1940, no /Users filesystem, NO outbound network of any kind). The MCP connection works regardless and is the only channel to the vault.

FIREFLIES DELETION IS OWNED ENTIRELY BY THE PRISM SERVER (as of 2026-07-09). It deletes a recording only after independently re-reading the vault and proving the verbatim body is stored. The old deletion queue, the zero-grace/aggressive-prune mode, and the Mac pruner (scripts/fireflies_prune.py, launchd) are all RETIRED — the pruner is disabled. This routine NEVER writes any `fireflies_*` field. Your only obligation toward deletion is to PRESERVE THE EVIDENCE the server's gate reads (Step 5); Step 9 is now read-only.

GOAL / END STATE: every processed transcript becomes a single canonical meeting note IN THE CORRECT PROJECT DIRECTORY (vault/projects/<slug>/meetings/<YYYY-MM-DD>_<slug>), or in vault/meetings/<YYYY-MM>/ when no project matches. The note holds an entity-extracted summary on top and the full verbatim transcript below, with people / orgs / projects linked as [[wikilinks]], action items as a checklist in the note (the commitments ledger, not this routine, turns them into tasks), and the note tagged `processed`. Organize by DIRECTORY PLACEMENT + a `projects:` wikilink, NEVER by a project-slug tag. Attribute all work to {{owner.name}}, never to an organization. Run autonomously; note any judgment calls in the run report.

## Step 1 — Find work (two sources, combine and cap at 50/run)

(a) New transcripts: query-notes { tag: "transcript", exclude_tags: ["processed","duplicate","fireflies-empty-source"], include_content: false, limit: 50 }.

(b) Backlog sweep: query-notes { tag: "transcript", path_prefix: "vault/_inbox/transcripts/", exclude_tags: ["duplicate","fireflies-empty-source"], include_content: false, limit: 50 }. These are inbox transcripts not yet routed into a project/meetings folder; some carry a STALE `processed` tag from an older in-place run — treat them as unprocessed and process them anyway. Skip any whose metadata already has `routed_to` set.

Merge (a)+(b), dedup by id, process up to 50. Keep each result's id, path, metadata (title, date, attendees, source, source_id, recording_id). If none, go straight to the report.

PROTECTED — RECOVERY STUBS. Some notes are stubs for recordings Fireflies left untranscribed; their audio was re-submitted and their real content lives in a SEPARATE note. They are tagged `fireflies-empty-source` and carry metadata `fireflies_status: "empty-source-recovered"`, `fireflies_verbatim_chars: 0`, and no `## Transcript` section. NEVER process, summarize, move, rewrite, tag `duplicate`, or delete-note them — even though a canonical note with the same title exists. That note IS their replacement (see `fireflies_superseded_by_note`). Deleting a stub makes the Prism Server re-upload the audio and re-exhaust the transcription quota, which is what caused the 2026-07 deadlock.

DEDUP GUARD (before processing each): determine whether a canonical copy already exists OUTSIDE _inbox. Check by recording_id/source_id and by query-notes { tag: "meeting", search: "<title>" } matched on date. If a project/meetings copy already exists, this inbox note is a REDUNDANT DUPLICATE: delete-note it (the canonical copy holds the full transcript) and skip. If unsure, tag it `duplicate` instead of deleting and skip. Also dedup inbox-vs-inbox: if two inbox notes share the same recording/meeting (e.g. a human-readable-named copy and a kebab-slug copy), keep the one with source_id, delete the other. This guard NEVER applies to `fireflies-empty-source` stubs (see above).

## Step 2 — Read each transcript body (MCP, not REST)

query-notes { id: PATH, include_content: true }.

- Small transcripts: use inline content.

- Large transcripts persist to a file. The body is a single JSON line — Read CANNOT sub-slice it and the sandbox shell CANNOT see it, so DO NOT use bash. Use the Grep tool on the persisted file path:

  1. Section headers: Grep { pattern: "## [A-Z][A-Za-z' ]+", path: FILE, output_mode: "content", -o: true }

  2. Fathom bullets: Grep { pattern: "\\[[^\\]]{25,}\\]\\(https://fathom", path: FILE, output_mode: "content", -o: true, head_limit: 120 }

  3. Meetily (no Fathom summary): Grep { pattern: "\\(\\d\\d:\\d\\d:\\d\\d\\): [^\\\\]{15,400}", path: FILE, output_mode: "content", -o: true, head_limit: 200 } (paginate with offset for the tail). The grep result re-persists as a line-separated file that Read CAN open.

## Step 3 — Match a project

Compare against query-notes { tag: "project" }. Score by, in priority order: (1) an attendee appears in project.collaborators/project.people; (2) recurring-title pattern (e.g. "Acme weekly ..." → acme; "Riverside Commons ..." → riverside-commons; "Grant sync" → grants); (3) project.keywords; (4) content keywords. Set project_match_confidence = high | medium | low.

- high / medium → destination vault/projects/<slug>/meetings/; set projects: ["[[vault/projects/<slug>/PROJECT]]"].

- low / none → destination vault/meetings/<YYYY-MM>/; add tag `needs-project-review`; record the best guess as `project_guess: "<slug>"` (text). Leave `projects` OUT (never `""`, never a folder link) until the project is confirmed.

Routing is by DIRECTORY + `projects` wikilink — do not add a project-slug tag.

## Step 4 — Extract entities (wikilinks)

- People: for each attendee resolve the canonical person note (search by surname; follow merged_into tombstones to the canonical; create only if none exists). Set metadata.last-contact = meeting date; add to `attendees` as [[wikilinks]].

- Orgs / concepts: link [[vault/organizations/…]] / [[vault/concepts/…]] inline where mentioned; create a concept stub only when novel and high-confidence.

- Action items: listed in the meeting note's ### Action Items checklist only. Do NOT create `task` notes, and do NOT create `promise` notes. Commitments and action items are extracted separately by the Omni bot's commitments ledger (scripts/ledger.py), which reads the same transcripts, routes {{owner.call_me}}'s own commitments into tasks, others' commitments into a waiting-on list, and dedups against existing tasks. Two writers made the task list 79% raw meeting snippets and other people's to-dos (audit 2026-09-30). Keep the ### Action Items checklist in the meeting note itself — that is the human-readable record, not the task store.

## Step 5 — Compose the note in place, then move it

Insert the intelligence block ABOVE the transcript:

- Fathom: content_edit old_text "## Summary\n\n## Meeting Purpose" → block that keeps "## Meeting Purpose" last.

- Meetily / Fireflies / no ## Summary anchor: insert immediately before "## Transcript" (or prepend).

The block, under ## Summary: attribution line "*Processed by {{agent.name}} for {{owner.name}} on <date>.*", prose summary, then ### Key Decisions, ### Action Items (checkboxes with **assignee**), ### Topics Discussed, ### Attendees (wikilinks).

CRITICAL — preserve verbatim: you INSERT above the transcript, never replace it. The full verbatim `## Transcript` section must remain intact. (This is exactly what makes a Fireflies recording safe for the server to delete — and what made old Fathom/Meetily summary-only stubs UNSAFE.)

THE EXACT CONTRACT. The Prism Server's delete gate re-reads this note directly. For a Fireflies recording to ever be deletable, ALL of the following must survive your edit:
- `metadata.source === "fireflies"` (that exact string) and `metadata.source_id` unchanged.
- A heading that is exactly `## Transcript`, alone on its own line — no suffix, no "(verbatim)", no trailing text. The server matches `/^##[ \t]+Transcript[ \t]*$/`.
- The full verbatim beneath it: at least 90% of `metadata.fireflies_verbatim_chars`.
If you truncate the body, drop or rename the heading, or alter `source`/`source_id`, the gate fails closed FOREVER: the recording is never deleted, Fireflies fills to its 400-minute cap, and it then silently stops transcribing new meetings.

Set metadata: type: meeting, status: processed, processed_by, processed_at, project_match_confidence, transcript_origin (old path), projects, attendees. Add tags `meeting` + `processed` (keep `transcript` and the source tag). Use include_content: false, force: true. Moving the note is safe — the server matches on `source_id`, not path, and note ids are stable across a move.

THEN MOVE the note: update-note { id, path: "<destination>/<YYYY-MM-DD>_<kebab-title>" }. The note ID is stable across the move, so all inbound links survive. Slug = <date>_<kebab-title>; never the timestamp-only Meetily/Fireflies filename.

## Step 6 — Cross-link the calendar stub (keep it separate)

Find the calendar note by title + date: query-notes { tag: "meeting", search: "<title>" }, match date. The transcript's meetingNoteId is UNRELIABLE — ignore it. On the matching calendar stub set status: "processed", projects, transcriptNoteId = <note id>, add a link → the canonical note (documented-by). On the canonical note set calendar_note. Don't merge.

If an update-note link write returns "null is not an object", ignore it — the write commits; verify with query-notes { id, include_links: true }.

## Step 7 — Run report

Create vault/agent/reports/<date>-transcript-processing (<date> = today in {{owner.timezone}} — this runs at 00:00 UTC, so the sandbox clock is already tomorrow) tagged `report`, metadata date, processed-by="{{owner.name}}", status="complete". For each transcript: destination path + project (+confidence), people created vs updated, calendar cross-linked (y/n), needs-project-review flag, and any deletes (duplicates removed). ALSO include a READ-ONLY Fireflies summary from query-notes { tag: "transcript", metadata: { source: "fireflies" } }: counts by `fireflies_delete_status` (deleted / blocked / unset), any `blocked` notes with their `fireflies_block_reason`, and any `fireflies-empty-source` stubs still lacking `fireflies_superseded_by_note`. Modify nothing while reporting. Attribute to {{owner.name}}, never an organization.

## Step 8 — No task or promise notes

Do NOT create `task` notes, and do NOT create `promise` notes. Commitments and action items are extracted separately by the Omni bot's commitments ledger (scripts/ledger.py), which reads the same transcripts, routes {{owner.call_me}}'s own commitments into tasks, others' commitments into a waiting-on list, and dedups against existing tasks. Two writers made the task list 79% raw meeting snippets and other people's to-dos (audit 2026-09-30). Keep the ### Action Items checklist in the meeting note itself — that is the human-readable record, not the task store.

## Step 9 — Fireflies metadata is READ-ONLY

The Prism Server owns Fireflies deletion. It re-reads each note itself and deletes a recording only when the note proves the verbatim body is stored AND the meeting is owned by the account's own API key. There is no queue to write. Never create, modify, or remove ANY of these fields:

  fireflies_delete_status, fireflies_deleted_at, fireflies_delete_after,
  fireflies_verified_at, fireflies_verbatim_chars, fireflies_block_reason,
  fireflies_status, fireflies_superseded_by_note, fireflies_recovered_from,
  source, source_id

Two of these will silently break the system if you write them:
- `fireflies_verbatim_chars` is written by the server at ingest and is the exact size of the source's verbatim. The gate requires the stored body to be ≥ 90% of it. Overwriting it with an estimate even slightly high permanently blocks that recording from ever being deleted.
- `fireflies_delete_status: "deleted"` makes the server flag a FALSE DELETE and relabel the note `blocked`.

You MAY read these fields for the run report (Step 7). That is all.

Two facts, corrected 2026-07-09: the storage cap is 400 minutes (not ~800), and it counts ONLY meetings this account owns — meetings organized by other people appear in the Fireflies transcript list but are billed to their owner and can never be deleted by this key.

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

## Guardrails

- Idempotent — dedup by recording_id/source_id and title+date globally (Step 1 guard); skip-if-exists for entities.

- NEVER write any `fireflies_*` field. The Prism Server owns them (Step 9).

- Delete inbox copies only when a canonical copy is confirmed elsewhere; otherwise tag `duplicate` — EXCEPT `fireflies-empty-source` stubs, which are never deleted, never tagged `duplicate`, and never processed.

- Stable slugs, never timestamp-only names.

- Canonical entities only — follow merged_into to the canonical note.

- No project-slug tags — placement is by directory + projects wikilink.

- Confidence-gated routing — low/none confidence → vault/meetings/<YYYY-MM>/ + needs-project-review; never force-fit into a project.

- Self-clearing: once a note is moved out of _inbox (or tagged `duplicate`), the Step 1 sweep won't re-pick it.

- FIREFLIES (verbatim-preservation non-negotiable): the Prism Server is the sole deleter. It re-reads the note itself and deletes a recording only when the stored body proves the verbatim was copied AND the meeting is owned by the account's own API key. Your entire contribution to that decision is Step 5: keep `source`, `source_id`, the exact `## Transcript` heading, and the full body. Never write a `fireflies_*` field (Step 9). Never touch `fireflies-empty-source` stubs. Never queue or delete Fathom/Meetily sources — their vault copies are often summary-only, so losing the source loses the verbatim forever.
