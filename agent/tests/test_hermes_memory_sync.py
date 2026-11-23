"""hermes_memory_sync: Hermes memory mirrored into Parachute, only on change, read-only toward Hermes. Offline."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hermes_memory_sync as sync_mod  # noqa: E402
import vault_shapes  # noqa: E402


class FakeClient:
    """Records writes; applies the same field-shape normalisation ParachuteClient does on every write."""
    def __init__(self):
        self.notes, self.writes = {}, []

    def get_note(self, path, include_content=True):
        n = self.notes.get(path)
        return SimpleNamespace(id='id-' + path, path=path, tags=list(n['tags'])) if n else None

    def create_note(self, content='', path=None, tags=None, metadata=None, links=None):
        self.writes.append(('create', path, metadata, tags))
        self.notes[path] = {'content': content, 'tags': list(tags or []), 'metadata': dict(metadata or {})}

    def update_note(self, note_id, content=None, metadata=None, tags_add=None, shape_tags=None, **kw):
        path = note_id[3:]
        self.writes.append(('update', path, metadata, tags_add))
        n = self.notes[path]
        n['content'] = content
        n['metadata'].update(metadata or {})
        n['tags'] += [t for t in tags_add or [] if t not in n['tags']]


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.home = root / 'hermes'
        (self.home / 'memories').mkdir(parents=True)
        self.memory = self.home / 'memories' / 'MEMORY.md'
        self.user = self.home / 'memories' / 'USER.md'
        self.memory.write_text('Alex prefers 60-minute meetings.\n')
        self.user.write_text('Alex Morgan, Springfield, MST.\n')
        self.state = root / 'state' / 'hermes-memory-sync.json'
        self.client = FakeClient()

    def tearDown(self):
        self.temp.cleanup()

    def run_sync(self, **kw):
        kw.setdefault('scrub', lambda text: text.replace('sk-planted-secret-0123456789', '[redacted]'))
        return {r['file']: r['status'] for r in sync_mod.sync(self.client, self.home, self.state, now='2026-10-23T10:00:00Z', **kw)}

    def test_first_run_creates_both_mirror_notes_tagged_agent_memory(self):
        self.assertEqual(self.run_sync(), {'MEMORY.md': 'written', 'USER.md': 'written'})
        mem = self.client.notes['vault/agent/memory/hermes-memory']
        usr = self.client.notes['vault/agent/memory/hermes-user']
        self.assertEqual(mem['tags'], ['agent-memory'])
        self.assertEqual(usr['tags'], ['agent-memory'])
        self.assertIn('Alex prefers 60-minute meetings.', mem['content'])
        self.assertEqual(mem['metadata']['hermesFile'], 'MEMORY.md')
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o600)

    def test_writes_only_when_the_content_changed(self):
        self.run_sync()
        self.assertEqual(len(self.client.writes), 2)
        self.assertEqual(self.run_sync(), {'MEMORY.md': 'unchanged', 'USER.md': 'unchanged'})
        self.assertEqual(len(self.client.writes), 2)
        self.memory.write_text('Alex prefers 60-minute meetings.\nNo calls on Fridays.\n')
        self.assertEqual(self.run_sync(), {'MEMORY.md': 'written', 'USER.md': 'unchanged'})
        self.assertEqual([w[0] for w in self.client.writes], ['create', 'create', 'update'])
        self.assertIn('No calls on Fridays.', self.client.notes['vault/agent/memory/hermes-memory']['content'])
        self.assertIsNone(self.client.writes[-1][3], 'the tag is already there; not re-added')

    def test_metadata_keeps_valid_field_shapes(self):
        self.run_sync()
        for _op, _path, meta, tags in self.client.writes:
            self.assertEqual(vault_shapes.normalize_metadata(dict(meta), tags or ['agent-memory'], 'create'), meta)
            self.assertTrue(all(v not in ('', None, [], ' ') for v in meta.values()), meta)
            self.assertTrue(all(isinstance(v, str) for v in meta.values()), meta)

    def test_a_missing_file_is_skipped_not_mirrored_empty(self):
        self.user.unlink()
        self.assertEqual(self.run_sync(), {'MEMORY.md': 'written', 'USER.md': 'missing'})
        self.assertNotIn('vault/agent/memory/hermes-user', self.client.notes)

    def test_a_failed_write_is_retried_next_run(self):
        class Down(FakeClient):
            def create_note(self, *a, **k): raise OSError('vault down')
        self.client = Down()
        self.assertEqual(self.run_sync(), {'MEMORY.md': 'failed', 'USER.md': 'failed'})
        self.client = FakeClient()
        self.assertEqual(self.run_sync(), {'MEMORY.md': 'written', 'USER.md': 'written'})

    def test_dry_run_writes_nothing_anywhere(self):
        self.assertEqual(self.run_sync(dry_run=True), {'MEMORY.md': 'would-write', 'USER.md': 'would-write'})
        self.assertEqual(self.client.writes, [])
        self.assertFalse(self.state.exists())

    def test_read_only_toward_hermes(self):
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.home / 'memories').iterdir()}
        self.run_sync()
        self.memory.write_text('changed\n')
        before['MEMORY.md'] = (self.memory.read_bytes(), self.memory.stat().st_mtime_ns)
        self.run_sync()
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.home / 'memories').iterdir()}
        self.assertEqual(before, after)

    def test_secrets_are_scrubbed_before_the_vault(self):
        self.memory.write_text('API key is sk-planted-secret-0123456789\n')
        self.run_sync()
        content = self.client.notes['vault/agent/memory/hermes-memory']['content']
        self.assertNotIn('sk-planted-secret-0123456789', content)
        self.assertIn('[redacted]', content)

    def test_without_a_scrubber_nothing_is_written(self):
        self.assertEqual(self.run_sync(scrub=None), {'MEMORY.md': 'failed', 'USER.md': 'failed'})
        self.assertEqual(self.client.writes, [])
        self.assertFalse(self.state.exists())

    def test_never_a_shared_team_vault(self):
        source = (Path(sync_mod.__file__)).read_text()
        # Only the configured vault (ParachuteClient); never a second, shared vault by URL.
        self.assertNotIn('https://', source)
        self.assertIn('ParachuteClient()', source)


if __name__ == '__main__':
    unittest.main()
