#!/usr/bin/env python3
"""hermes_memory_sync — mirror Hermes' native memory into Parachute (owner decision 4, 2026-10-22).

Hermes memory (~/.hermes/memories/MEMORY.md and USER.md) is a first-class store: Omni reads and writes it every
turn, and proactive runs write it too. Parachute holds a MIRROR so every other surface sees the same facts:

    ~/.hermes/memories/MEMORY.md  ->  vault/agent/memory/hermes-memory   (tag agent-memory)
    ~/.hermes/memories/USER.md    ->  vault/agent/memory/hermes-user     (tag agent-memory)

One direction only: this never writes, locks or touches anything under ~/.hermes. A note is written only when the
file's content changed since the last mirror (sha256 kept in state/hermes-memory-sync.json, 0600, atomic), so the
hourly job is a no-op most hours. A missing file is skipped, never mirrored as empty. The text is passed through
omni-bridge's secret scrubber (known secret VALUES replaced) before it reaches the vault, which cloud routines and
connectors read; without the scrubber nothing is written (status failed, exit 1). Writes go through
ParachuteClient (field-shape guard) into the configured vault only; never a second, shared vault.

    python3 scripts/hermes_memory_sync.py            # mirror what changed
    python3 scripts/hermes_memory_sync.py --dry-run  # say what would be written
Registered hourly by `scripts/install_proactive.py --memory-sync`.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TAG = 'agent-memory'
MIRRORS = (('MEMORY.md', 'vault/agent/memory/hermes-memory', 'Hermes memory (MEMORY.md)'),
           ('USER.md', 'vault/agent/memory/hermes-user', 'Hermes user profile (USER.md)'))
DEFAULT_STATE = HERE.parent / 'state' / 'hermes-memory-sync.json'


def hermes_home(environ=os.environ):
    value = (environ.get('HERMES_HOME') or '').strip()
    return Path(value).expanduser() if value else Path.home() / '.hermes'


def load_state(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, data):
    """0600, temp file then rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write('\n')
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def note_body(title, source, text):
    return ('# %s\n\nMirror of `%s`, written by scripts/hermes_memory_sync.py. Hermes memory is the store; edit it '
            'there (Omni\'s `memory` tool), not here: this note is overwritten when the file changes.\n\n---\n\n%s'
            % (title, source, text.rstrip() + '\n'))


def metadata(name, digest, now):
    return {'source': 'hermes', 'hermesFile': name, 'contentHash': digest, 'mirroredAt': now}


def sync(client, home, state_path, now=None, dry_run=False, scrub=None):
    """Mirror each changed memory file. Returns one result per file: unchanged | missing | written | would-write | failed."""
    now = now or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    state = load_state(state_path)
    hashes = dict(state.get('hashes') or {})
    results = []
    for name, path, title in MIRRORS:
        source = Path(home) / 'memories' / name
        try:
            raw = source.read_bytes()
        except FileNotFoundError:
            results.append({'file': name, 'note': path, 'status': 'missing'})
            continue
        text = raw.decode('utf-8', errors='replace')
        digest = hashlib.sha256(raw).hexdigest()
        if hashes.get(name) == digest:
            results.append({'file': name, 'note': path, 'status': 'unchanged'})
            continue
        if dry_run:
            results.append({'file': name, 'note': path, 'status': 'would-write', 'chars': len(text)})
            continue
        if scrub is None:
            results.append({'file': name, 'note': path, 'status': 'failed', 'error': 'secret scrubber unavailable'})
            continue
        try:
            body, meta = note_body(title, '~/.hermes/memories/' + name, scrub(text)), metadata(name, digest, now)
            existing = client.get_note(path, include_content=False)
            if existing is None:
                client.create_note(content=body, path=path, tags=[TAG], metadata=meta)
            else:
                tags = list(getattr(existing, 'tags', None) or [])
                client.update_note(existing.id, content=body, metadata=meta, tags_add=None if TAG in tags else [TAG],
                                   shape_tags=[TAG])
        except Exception as error:
            results.append({'file': name, 'note': path, 'status': 'failed', 'error': type(error).__name__})
            continue
        hashes[name] = digest
        results.append({'file': name, 'note': path, 'status': 'written', 'chars': len(text)})
    if not dry_run and hashes != (state.get('hashes') or {}):
        save_state(state_path, {'hashes': hashes, 'lastWrite': now})
    return results


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--hermes-home', type=Path, help='default $HERMES_HOME or ~/.hermes')
    p.add_argument('--state', type=Path, default=DEFAULT_STATE)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args(argv)
    os.umask(0o077)
    sys.path.insert(0, str(HERE))
    from parachute_client import ParachuteClient
    from proactive_intelligence.learning_audit import _scrubber
    results = sync(ParachuteClient(), a.hermes_home or hermes_home(), a.state, dry_run=a.dry_run, scrub=_scrubber())
    print(json.dumps(results, indent=2))
    # A failed mirror is a non-zero exit, so the Hermes job's last_status shows it.
    return 1 if any(r['status'] == 'failed' for r in results) else 0


if __name__ == '__main__':
    sys.exit(main())
