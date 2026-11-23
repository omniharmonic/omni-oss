# Tag Schemas

These schemas define expected metadata fields for each tag. Claude sees these
schemas when working with tagged notes, which helps it write correct metadata.

**This file is the source of truth. To apply it, run the schema-bridge:**

```bash
python3 scripts/apply_tag_schemas.py            # validate + apply all schemas
python3 scripts/apply_tag_schemas.py --dry-run  # validate + show diff only
```

The bridge enforces two Parachute 0.5.x rules: only `string`/`integer`/`boolean`
fields may be `indexed`, and every tag sharing a field name must agree on that
field's type + indexed flag.

## How to apply manually

Parachute 0.5.x REST lives under `/vault/{vault}/api` and only accepts
hub-minted JWTs (`parachute auth mint-token --scope vault:default:write`):

```bash
curl -X PUT \
  -H "Authorization: Bearer $(parachute auth mint-token --scope vault:default:write)" \
  -H "Content-Type: application/json" \
  "http://127.0.0.1:1940/vault/default/api/tags/{tag-name}" \
  -d '{"description": "...", "fields": {...}}'
```

Or, interactively in Claude Code, the MCP tool `update-tag`.

---

## task

> **0.5.x field names (handoff 2026-06-02 §2).** Use `status`/`due`/`assigned`/
> `priority`/`completed`/`context` — NOT the legacy `pending`/`deadline`/`requester`.
> `priority` is the one indexed task field. (The local `state/active-tasks.json`
> queue keeps its own legacy schema — this applies only to vault notes tagged `task`.)

```json
{
  "description": "Action items, commitments, and work to be done. Lifecycle: todo → in-progress → done (or blocked/cancelled). Link tasks to the project and person they relate to.",
  "fields": {
    "status": {
      "type": "string",
      "description": "Task lifecycle status",
      "enum": ["todo", "in-progress", "blocked", "done", "cancelled"]
    },
    "priority": {
      "type": "string",
      "description": "Priority level (indexed — queryable with operators)",
      "indexed": true,
      "enum": ["critical", "high", "medium", "low"]
    },
    "due": {
      "type": "string",
      "description": "ISO-8601 date/datetime the task is due, or '' for none"
    },
    "assigned": {
      "type": "string",
      "description": "Who owns the task (default: The owner)"
    },
    "completed": {
      "type": "string",
      "description": "ISO-8601 date the task was completed (set when status=done)"
    },
    "context": {
      "type": "string",
      "description": "Where the task came from / supporting notes"
    }
  }
}
```

## person

```json
{
  "description": "A person the owner interacts with. Store contact info, role, and relationship context. Link to their organizations and projects.",
  "fields": {
    "role": {
      "type": "string",
      "description": "Professional role or title"
    },
    "email": {
      "type": "string",
      "description": "Primary email address"
    },
    "phone": {
      "type": "string",
      "description": "Phone number"
    },
    "telegram": {
      "type": "string",
      "description": "Telegram handle"
    },
    "relationship_type": {
      "type": "string",
      "description": "Relationship to the owner",
      "enum": ["collaborator", "stakeholder", "contact", "advisor", "funder"]
    },
    "timezone": {
      "type": "string",
      "description": "IANA timezone (e.g. Europe/Berlin)"
    },
    "last-contact": {
      "type": "string",
      "description": "ISO-8601 date of last interaction (canonical name is hyphenated)"
    }
  }
}
```

> Organizations/projects/aliases are wikilink-string arrays declared by Prism
> (`organizations`, `projects`, `aliases`) plus `contact`, `first-met`, `source`,
> `confidence`, `location`. The EA layer adds `ea_managed`, `priority_weight`,
> `cadence_baseline_days`, `relationship_status`, `interaction_score_90d` — see
> `.claude/planning/executive-assistant-agent.md` §5 (canonical for EA fields).

## project

```json
{
  "description": "A project the owner is involved in. Contains goals, status, collaborators, and links to related meetings and tasks.",
  "fields": {
    "status": {
      "type": "string",
      "description": "Project lifecycle status",
      "enum": ["active", "paused", "completed", "archived"]
    },
    "role": {
      "type": "string",
      "description": "The owner's role in this project",
      "enum": ["lead", "contributor", "advisor", "observer"]
    },
    "org": {
      "type": "string",
      "description": "Parent organization (wikilink string; canonical key is 'org')"
    },
    "keywords": {
      "type": "string",
      "description": "Comma-separated keywords for matching (e.g. 'bioregional, food chain, regenerative agriculture')"
    }
  }
}
```

## transcript

> **0.5.x field names (handoff §2).** Use `source_id`/`synced_at` — NOT the
> legacy camelCase `sourceId`/`fathomUrl`.

```json
{
  "description": "A meeting transcript from Fathom or Meetily. Process by extracting attendees, action items, and decisions, then add the 'processed' tag.",
  "fields": {
    "source": {
      "type": "string",
      "description": "Transcript source",
      "enum": ["fathom", "meetily", "fireflies"]
    },
    "source_id": {
      "type": "string",
      "description": "Recording/meeting ID from the source platform"
    },
    "date": {
      "type": "string",
      "description": "Meeting date (ISO-8601)"
    },
    "duration_minutes": {
      "type": "integer",
      "description": "Meeting length in minutes"
    },
    "synced_at": {
      "type": "string",
      "description": "ISO-8601 timestamp the transcript was pulled into the vault"
    }
  }
}
```

## email

```json
{
  "description": "An email thread synced from Gmail. Classified with importance tags (urgent, action-required, informational).",
  "fields": {
    "from": {
      "type": "string",
      "description": "Sender display name and email"
    },
    "subject": {
      "type": "string",
      "description": "Email subject line"
    },
    "threadId": {
      "type": "string",
      "description": "Gmail thread ID"
    },
    "date": {
      "type": "string",
      "description": "Most recent message date"
    },
    "isUnread": {
      "type": "boolean",
      "description": "Whether the thread has unread messages"
    },
    "messageCount": {
      "type": "integer",
      "description": "Number of messages in thread"
    }
  }
}
```

## meeting

> **0.5.x field names (handoff §2).** Processing lifecycle is `status` ∈
> {raw, cleaned, processed}; calendar lifecycle is the separate `event_status`.
> `projects`/`attendees` are wikilink-string arrays; `fathom_url`/`recording_id`
> carry the source linkage (the transcript's own id lives on the transcript note).

```json
{
  "description": "A meeting note (often paired with a transcript). Links to attendee person notes and project notes. status tracks processing; event_status tracks the calendar lifecycle.",
  "fields": {
    "date": {
      "type": "string",
      "description": "Meeting date (ISO-8601)"
    },
    "status": {
      "type": "string",
      "description": "Processing lifecycle",
      "enum": ["raw", "cleaned", "processed"]
    },
    "event_status": {
      "type": "string",
      "description": "Calendar lifecycle (for events synced from Google Calendar)",
      "enum": ["confirmed", "tentative", "cancelled"]
    },
    "source": {
      "type": "string",
      "description": "Where the meeting note originated (fathom, meetily, calendar, manual)"
    },
    "recording_id": {
      "type": "string",
      "description": "Recording ID from the source platform"
    },
    "fathom_url": {
      "type": "string",
      "description": "Fathom share URL, when source is fathom"
    }
  }
}
```

## message-thread

```json
{
  "description": "A chat conversation from WhatsApp, Telegram, or other bridged platform. Classify with importance tags.",
  "fields": {
    "platform": {
      "type": "string",
      "description": "Message platform",
      "enum": ["whatsapp", "telegram", "signal", "discord", "email"]
    },
    "participants": {
      "type": "string",
      "description": "Comma-separated participant names"
    },
    "lastMessageAt": {
      "type": "string",
      "description": "Timestamp of most recent message"
    }
  }
}
```

## briefing

```json
{
  "description": "A daily briefing generated by the intelligence system. One per day at vault/agent/briefings/{date}.",
  "fields": {
    "date": {
      "type": "string",
      "description": "Briefing date (YYYY-MM-DD)"
    }
  }
}
```

## agent-insight

```json
{
  "description": "Intelligence analysis output. Patterns, escalations, and opportunities detected by the intelligence scan.",
  "fields": {
    "date": {
      "type": "string",
      "description": "Analysis date (YYYY-MM-DD)"
    }
  }
}
```
