"""
Standing Agent — the Executive Assistant Agent layer.

This package implements the EA Agent design from
.claude/planning/executive-assistant-agent.md.

Modules:
  bootstrap   — one-shot world-model bootstrap analyzer
  writers     — strongly-typed Parachute writers for new note types
  queries     — section queries used by active_packet
  importance  — multi-signal importance scorer (Phase 10)
  active_packet — materialized context packet assembly (Phase 8)
  rhythms     — rhythm detection + scheduling (Phase 13)
  archive     — JSONL archive writer + reader
  calibration — weight adjustments from /missed (Phase 12)

Source-of-truth design doc:
  .claude/planning/executive-assistant-agent.md
"""

__version__ = "0.1.0"
