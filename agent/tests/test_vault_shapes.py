"""Writer-side vault hygiene: the shape guard and the ParachuteWriter fixes.

Offline only — a fake client records what would be sent; no vault is touched.

    python3 tests/test_vault_shapes.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import parachute_client  # noqa: E402
import vault_shapes  # noqa: E402
from parachute_client import Note, ParachuteClient, ParachuteError  # noqa: E402
from parachute_writer import ParachuteWriter, project_note_path  # noqa: E402

import os  # noqa: E402

import apply_tag_schemas  # noqa: E402
import gen_field_shapes  # noqa: E402
import sync_routines  # noqa: E402


def _prism_checkout() -> Path | None:
    """A Prism checkout that carries the field-shape contract, if one is around:
    $PRISM_REPO first, then the usual places beside this repo / under ~/dev."""
    candidates = [os.environ.get("PRISM_REPO"), ROOT.parent / "prism", Path.home() / "dev" / "prism",
                  Path.home() / "dev" / "prism-ios"]
    for c in candidates:
        if c and (Path(c) / gen_field_shapes.PRISM_CONTRACT).exists():
            return Path(c)
    return None


PRISM = _prism_checkout()
SCHEMA_FIXES = (PRISM or ROOT.parent / "prism") / "scripts" / "vault-hygiene" / "schema-fixes.json"


class FakeClient:
    """Stores notes by path; records create/update payloads after shaping,
    exactly as ParachuteClient would send them."""

    def __init__(self, existing: dict[str, dict] | None = None):
        self.notes = {p: Note(id=f"id-{p}", path=p, tags=v.get("tags", []), metadata=v.get("metadata", {}),
                              content=v.get("content", ""), )
                      for p, v in (existing or {}).items()}
        self.creates: list[dict] = []
        self.updates: list[dict] = []

    def get_note(self, path, include_content=True):
        if path in self.notes:
            return self.notes[path]
        raise ParachuteError(404, "not found")

    def create_note(self, content="", path=None, tags=None, metadata=None, links=None):
        metadata = parachute_client._shape(metadata, tags)
        self.creates.append({"path": path, "tags": tags, "metadata": metadata})
        n = Note(id=f"id-{path}", path=path, tags=tags or [], metadata=metadata or {}, content=content,
                 )
        self.notes[path] = n
        return n

    def update_note(self, note_id, **kw):
        shape_tags = kw.pop("shape_tags", None)
        kw["metadata"] = parachute_client._shape(
            kw.get("metadata"), [*(shape_tags or []), *(kw.get("tags_add") or [])] or None, "update")
        self.updates.append({"id": note_id, **kw})
        return None


def _no_empty_strings(md: dict) -> bool:
    return all(v != "" for v in md.values()) and all(
        "" not in v for v in md.values() if isinstance(v, list))


class GuardTests(unittest.TestCase):
    def test_client_create_and_update_are_shaped(self):
        sent = []
        c = ParachuteClient.__new__(ParachuteClient)
        with patch.object(ParachuteClient, "_request", lambda self, m, p, body=None, **k: sent.append(body) or {
                "id": "x", "path": "p", "tags": [], "metadata": {}, "content": ""}):
            c.create_note(path="vault/people/a", tags=["person"],
                          metadata={"aliases": "", "organizations": "", "confidence": 0.91})
            c.update_note("x", metadata={"projects": "", "title": "T"})
        self.assertEqual(sent[0]["metadata"], {"confidence": "high"})
        self.assertEqual(sent[1]["metadata"], {"projects": None, "title": "T"})

    def test_enums_match_schema_fixes(self):
        """The guard's vocabularies are the corrected schemas' (schema-fixes.json)."""
        if not SCHEMA_FIXES.exists():
            self.skipTest("prism checkout not beside this repo")
        fixes = {c["id"]: c for c in json.loads(SCHEMA_FIXES.read_text())["changes"]}
        self.assertEqual(set(fixes["task-status"]["to"]["enum"]), set(vault_shapes.TASK_STATUSES))
        self.assertEqual(set(fixes["thread-platform"]["to"]["enum"]), set(vault_shapes.THREAD_PLATFORMS))
        self.assertEqual(tuple(sorted(fixes["person-confidence"]["to"]["enum"])),
                         tuple(sorted(vault_shapes.CONFIDENCE_LABELS)))
        self.assertEqual(fixes["thread-participants"]["to"]["type"], "array")
        self.assertIn("participants", vault_shapes.LIST_FIELDS_BY_TAG["message-thread"])
        for syn, canon in vault_shapes.TASK_STATUS_SYNONYMS.items():
            self.assertIn(canon, vault_shapes.TASK_STATUSES, syn)


def _stale_in(path: Path, block: str) -> list[str]:
    """gen_field_shapes.stale() for one file, without the file-name prefix."""
    with patch.object(gen_field_shapes, "prompt_files", lambda: [path]), \
            patch.object(gen_field_shapes, "REPO", path.parents[2]), \
            patch.object(gen_field_shapes, "BLOCK_PATH", path.parents[2] / "block.md"):
        (path.parents[2] / "block.md").write_text(block + "\n")
        return [line.split(": ", 1)[1] for line in gen_field_shapes.stale(block)]


class ContractTests(unittest.TestCase):
    """One contract (config/vault-shapes.json) → the guard, the prompt block, the routines."""

    def test_guard_data_is_the_contract(self):
        c = json.loads((ROOT / "config" / "vault-shapes.json").read_text())
        self.assertEqual({t: set(f) for t, f in c["listFields"].items()},
                         {t: set(f) for t, f in vault_shapes.LIST_FIELDS_BY_TAG.items()})
        self.assertEqual(set(c["taskStatus"]["values"]), set(vault_shapes.TASK_STATUSES))
        self.assertEqual(c["taskStatus"]["synonyms"], vault_shapes.TASK_STATUS_SYNONYMS)
        self.assertEqual(set(c["threadPlatforms"]), set(vault_shapes.THREAD_PLATFORMS))
        self.assertEqual(tuple(c["confidence"]["labels"]), vault_shapes.CONFIDENCE_LABELS)
        self.assertEqual(set(c["confidence"]["tags"]), set(vault_shapes.CONFIDENCE_TAGS))
        # Behaviour driven by the contract's own data: a rule added to the JSON is exercised.
        for tag, fields in c["listFields"].items():
            dirty = {f: ("" if i % 2 else "one") for i, f in enumerate(fields)}
            shaped = vault_shapes.normalize_metadata(dirty, [tag])
            for f in fields:
                self.assertTrue(f not in shaped or isinstance(shaped[f], list), (tag, f, shaped))
        for f in c["textFields"]["*"]:
            self.assertEqual(vault_shapes.normalize_metadata({f: 12}, None, "update")[f], "12")
        for tag, fields in c["integerFields"].items():
            for f in fields:
                self.assertEqual(vault_shapes.normalize_metadata({f: "2026-10-01T00:00:00Z"}, [tag])[f], 1790812800000)

    def test_block_is_rendered_from_the_contract(self):
        block = gen_field_shapes.render_block()
        self.assertEqual((ROOT / "config" / "vault-field-shapes.md").read_text(), block + "\n",
                         "stale: python3 scripts/gen_field_shapes.py --write")
        self.assertIn(gen_field_shapes.body_sha256()[:12], block.splitlines()[0])
        # The marker hashes the block's TEXT: a contract key no prompt reads (a lint tag)
        # must not change the block, or every routine would need a re-sync for nothing.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "c.json"
            c = json.loads((ROOT / "config" / "vault-shapes.json").read_text())
            c["lintTags"] = [*c["lintTags"], "some-new-tag"]
            other.write_text(json.dumps(c))
            self.assertEqual(gen_field_shapes.render_block(other), block)
            c["confidence"]["labels"] = ["sure", "unsure"]
            other.write_text(json.dumps(c))
            self.assertNotEqual(gen_field_shapes.render_block(other), block)
        body = gen_field_shapes.render_body(vault_shapes.CONTRACT)
        self.assertNotRegex(body.replace('query-notes { id: "vault/projects/<slug>/PROJECT" }', ""), r"\{[A-Za-z]+\}",
                            "an unfilled {placeholder}")
        for tag, fields in vault_shapes.CONTRACT["listFields"].items():
            self.assertIn(f"{tag}:", body)
            for f in fields:
                self.assertIn(f"`{f}`", body)
        for word in [*vault_shapes.CONFIDENCE_LABELS, *vault_shapes.TASK_STATUSES]:
            self.assertIn(f"`{word}`", body)

    def test_every_routine_and_prompt_carries_the_current_block(self):
        self.assertEqual(gen_field_shapes.stale(), [])
        names = {p.parent.name for p in gen_field_shapes.prompt_files() if p.name == "SKILL.md"}
        self.assertEqual(names, {p.name for p in (ROOT / "routines").iterdir() if (p / "SKILL.md").exists()},
                         "a routine was added without the block")
        self.assertGreaterEqual(len(names), 6)  # the six routine templates in routines/
        block = gen_field_shapes.render_block()
        for p in gen_field_shapes.prompt_files():
            text = p.read_text()
            self.assertEqual(text.count(gen_field_shapes.BEGIN), 1, p)
            self.assertIn(block, text, p)
            # The block reaches the cloud prompt: it is in the body sync_routines sends.
            if p.name == "SKILL.md":
                self.assertIn(block, sync_routines.parse(p)["body"], p)

    def test_a_stale_or_hand_edited_block_is_caught(self):
        block = gen_field_shapes.render_block()
        edited = block.replace("`high`", "`certain`")
        self.assertNotEqual(edited, block)
        self.assertEqual(gen_field_shapes.with_block(f"a\n{edited}\nb", block), f"a\n{block}\nb")
        self.assertIsNone(gen_field_shapes.with_block("no markers here", block))
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "routines" / "meeting-prep" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text((ROOT / "routines" / "meeting-prep" / "SKILL.md").read_text().replace("`high`", "`certain`"))
            self.assertEqual(_stale_in(target, block), ["field-shapes block is stale"])
            target.write_text("a routine with no block at all")
            self.assertEqual(_stale_in(target, block), ["has no field-shapes block"])
        # sync_routines --check is wired to the same check.
        with patch.object(gen_field_shapes, "stale", lambda: ["routines/x/SKILL.md: field-shapes block is stale"]):
            self.assertEqual(sync_routines.shapes_drift(), ["routines/x/SKILL.md: field-shapes block is stale"])
            with patch("builtins.print"):
                self.assertGreaterEqual(sync_routines.check(None), 1, "a stale block makes --check fail")

    def test_no_prompt_teaches_a_folder_project_link_or_a_string_list(self):
        import re
        folder = re.compile(r"\[\[vault/projects/[^\]/|#]+\]\]")
        for p in gen_field_shapes.prompt_files():
            for line in p.read_text().splitlines():
                if folder.search(line):
                    self.assertRegex(line, r"(?i)never|resolves to nothing|legacy", f"{p.name}: {line[:90]}")
        prep = (ROOT / "routines" / "meeting-prep" / "SKILL.md").read_text()
        self.assertNotIn('"attendees": "{comma-separated', prep)
        self.assertIn('"attendees": [', prep)

    def test_vendored_files_match_the_prism_checkout(self):
        if PRISM is None:
            self.skipTest("no Prism checkout with the contract found (set PRISM_REPO to check)")
        self.assertEqual(gen_field_shapes.compare_with_prism(PRISM), [])

    def test_update_with_known_tags_gets_the_tag_rules(self):
        sent = []
        c = ParachuteClient.__new__(ParachuteClient)
        with patch.object(ParachuteClient, "_request", lambda self, m, p, body=None, **k: sent.append(body) or {
                "id": "x", "path": "p", "tags": [], "metadata": {}, "content": ""}):
            c.update_note("x", metadata={"confidence": 0.91, "aliases": "Sam"})
            c.update_note("x", metadata={"confidence": 0.91, "aliases": "Sam"}, shape_tags=["person"])
            c.update_note("x", metadata={"status": "Tracked"}, shape_tags=["task"])
        self.assertEqual(sent[0]["metadata"], {"confidence": 0.91, "aliases": "Sam"}, "by-name rules only")
        self.assertEqual(sent[1]["metadata"], {"confidence": "high", "aliases": ["Sam"]})
        self.assertEqual(sent[2]["metadata"], {"status": "pending"})
        for body in sent:
            self.assertNotIn("shape_tags", body, "never sent to the vault")

    def test_writer_updates_pass_the_note_tags_to_the_guard(self):
        fake = FakeClient({"vault/people/sam": {"tags": ["person"], "metadata": {"role": "x"}}})
        ParachuteWriter(client=fake).upsert_person("Sam", confidence="0.95", aliases=["Sammy"])
        md = fake.updates[-1]["metadata"]
        self.assertEqual(md.get("confidence"), "high")
        self.assertEqual(md.get("aliases"), ["Sammy"])

    def test_a_broken_guard_warns_once_instead_of_failing_silently(self):
        import io
        from contextlib import redirect_stderr

        class Boom:
            @staticmethod
            def normalize_metadata(*_a, **_k):
                raise RuntimeError("contract missing")

        buf = io.StringIO()
        with patch.object(parachute_client, "_SHAPES", Boom), patch.object(parachute_client, "_SHAPE_WARNED", False), \
                redirect_stderr(buf):
            self.assertEqual(parachute_client._shape({"a": ""}, ["person"]), {"a": ""}, "fails open")
            parachute_client._shape({"a": ""}, ["person"])
        self.assertEqual(buf.getvalue().count("shape guard is not running"), 1)

    def test_tag_schema_bridge_refuses_a_doc_that_contradicts_the_contract(self):
        schemas = apply_tag_schemas.parse_schemas(ROOT / "docs" / "tag-schemas.md")
        conflicts = apply_tag_schemas.contract_conflicts(schemas)
        self.assertTrue(any("task.status" in c for c in conflicts), conflicts)
        ok = {"task": {"fields": {"status": {"type": "string", "enum": sorted(vault_shapes.TASK_STATUSES)}}},
              "project": {"fields": {"keywords": {"type": "array"}}}}
        self.assertEqual(apply_tag_schemas.contract_conflicts(ok), [])
        with patch.object(sys, "argv", ["apply_tag_schemas.py"]), \
                patch.object(apply_tag_schemas, "apply", side_effect=AssertionError("must not reach the vault")):
            self.assertEqual(apply_tag_schemas.main(), 3)


class WriterTests(unittest.TestCase):
    def test_person_create_has_no_placeholders_and_links_project_note(self):
        fake = FakeClient()
        w = ParachuteWriter(client=fake)
        w.upsert_person("Sarah Chen", projects=["Riverside Commons"], confidence="0.7")
        md = fake.creates[-1]["metadata"]
        self.assertTrue(_no_empty_strings(md), md)
        self.assertNotIn("organizations", md)
        self.assertNotIn("aliases", md)
        self.assertEqual(md["projects"], ["[[vault/projects/riverside-commons/PROJECT]]"])
        self.assertEqual(md["confidence"], "medium")

    def test_legacy_project_note_at_folder_path_is_honoured(self):
        fake = FakeClient({"vault/projects/legacy": {"tags": ["project"]}})
        w = ParachuteWriter(client=fake)
        self.assertEqual(w.project_link("legacy"), "[[vault/projects/legacy]]")
        self.assertEqual(w.project_link("newone"), "[[vault/projects/newone/PROJECT]]")

    def test_new_project_is_created_at_PROJECT(self):
        fake = FakeClient()
        ParachuteWriter(client=fake).upsert_project("Bio Food", collaborators=["Ann Lee"])
        self.assertEqual(fake.creates[-1]["path"], project_note_path("bio-food"))
        self.assertTrue(_no_empty_strings(fake.creates[-1]["metadata"]))

    def test_meeting_link_recording_and_source(self):
        fake = FakeClient()
        ParachuteWriter(client=fake).upsert_meeting(
            title="Sync", date="2026-10-01", project_slug="riverside-commons", source="Fathom",
            recording_id=98765)
        md = fake.creates[-1]["metadata"]
        self.assertEqual(md["projects"], ["[[vault/projects/riverside-commons/PROJECT]]"])
        self.assertEqual(md["recording_id"], "98765")
        self.assertEqual(md["source"], "fathom")
        self.assertTrue(_no_empty_strings(md))

    def test_task_status_vocabulary_and_no_status_tag(self):
        fake = FakeClient()
        w = ParachuteWriter(client=fake)
        w.upsert_task("Send spec", status="todo", project_slug="riverside-commons")
        c = fake.creates[-1]
        self.assertEqual(c["metadata"]["status"], "pending")
        self.assertEqual(c["tags"], ["task", "riverside-commons"])
        self.assertEqual(c["metadata"]["project"], "[[vault/projects/riverside-commons/PROJECT]]")
        w.upsert_task("Ship it", status="done")
        self.assertTrue(fake.creates[-1]["path"].startswith("vault/tasks/done/"))
        self.assertEqual(fake.creates[-1]["metadata"]["status"], "completed")

    def test_update_never_writes_empty(self):
        fake = FakeClient({"vault/people/sam": {"tags": ["person"], "metadata": {"role": "x"}}})
        ParachuteWriter(client=fake).upsert_person("Sam", role="", aliases=[])
        for u in fake.updates:
            md = u.get("metadata") or {}
            self.assertTrue(_no_empty_strings(md))
            self.assertNotIn("role", md)  # an empty argument never blanks a stored value


if __name__ == "__main__":
    unittest.main(verbosity=1)
