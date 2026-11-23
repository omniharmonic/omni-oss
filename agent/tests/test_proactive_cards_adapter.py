"""Offline fixture of the cards-mode Hermes run: read-only vault tools, Hermes memory and skills (audited, skill writes
confined to the local skills folder), background review, bounded. No network.

The fake AIAgent mirrors the real Hermes behaviours that matter for safety (hermes-src-mini):
- the turn-start MCP refresh re-publishes the full tool list unless agent._skip_mcp_refresh (turn_context.py:551);
- every model call receives the offered tools in api_kwargs['tools'];
- an unknown tool name goes to _repair_tool_call and is never dispatched (conversation_loop.py:7062);
- any exception inside the loop is caught and the conversation continues (conversation_loop.py:8426);
- the memory tool calls agent._memory_store.add/replace/remove/apply_batch; skill_manage goes through the
  module-global tools.skill_manager_tool.skill_manage (the registry lambda), which finds skills in the local
  folder first, then skills.external_dirs (skill_manager_tool._find_skill);
- the post-turn background review runs in a daemon thread named 'bg-review', is a NEW AIAgent sharing the
  parent's memory store, and fires on the nudge counters (turn_finalizer.py:781, background_review.py:1328).
"""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from proactive_intelligence import hermes_adapter
from proactive_intelligence.hermes_adapter import HermesReasoner, READ_ONLY_TOOLS, LEARNING_TOOLS, extract_json_object

LEARN = ('memory', 'skills_list', 'skill_view', 'skill_manage')
AGENT = '''import json, os, threading, time
from types import SimpleNamespace
# Every tool a fully loaded Hermes could hand over, per model name. Only the vault read tools and Hermes' own
# memory and skills tools may be offered.
LEARN = ('memory', 'skills_list', 'skill_view', 'skill_manage')
OFFERED = {
    'fixture-model': ('mcp_parachute_query_notes', 'mcp_parachute_update_note', 'mcp_parachute_create_note', 'terminal') + LEARN,
    'double-underscore': ('mcp__parachute__query_notes', 'mcp__parachute__find_path', 'mcp__parachute__vault_info',
                          'mcp__parachute__update_note', 'mcp__parachute__delete_note', 'terminal', 'write_file', 'web_search', 'send_message') + LEARN,
    'no-vault': ('terminal', 'read_file') + LEARN,
}
EXPECTED = {
    'double-underscore': ['mcp__parachute__query_notes', 'mcp__parachute__find_path', 'mcp__parachute__vault_info'] + list(LEARN),
    'no-vault': list(LEARN),
}
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'memory.log')
def note(what):
    with open(LOG, 'a') as f: f.write(what + '\\n')
class FakeMemoryManager:
    def prefetch_all(self, query, *, session_id=''): note('recall:prefetch_all'); return 'Alex prefers mornings'
    def build_system_prompt(self): note('recall:build_system_prompt'); return 'memory block'
    def on_turn_start(self, *a, **k): note('provider:on_turn_start')
    def sync_all(self, *a, **k): note('provider:sync_all')
class FakeMemoryStore:
    def load_from_disk(self): pass
    def target_enabled(self, target): return True
    def format_for_system_prompt(self, target): note('recall:store'); return 'MEMORY.md'
    def save_to_disk(self, target): note('write:store.save_to_disk')
    def add(self, target, content):
        if os.environ.get('FIXTURE_AUDIT_PATH'):  # how many audit lines existed when the write itself ran
            with open(os.environ['FIXTURE_AUDIT_PATH']) as f: note('audit-lines-at-write:%d' % len(f.read().splitlines()))
        note('write:store.add:' + target); return {'success': True}
    def replace(self, target, old_text, new_content): note('write:store.replace'); return {'success': True}
    def remove(self, target, old_text): note('write:store.remove'); return {'success': True}
    def apply_batch(self, target, operations): note('write:store.apply_batch'); return {'success': bool(operations)}
class StoreWithNewWriter(FakeMemoryStore):
    def append_journal(self, *a, **k): note('write:store.append_journal')
class StoreWithoutBatch(FakeMemoryStore):
    apply_batch = None
class AIAgent:
    def __init__(self, **kwargs):
        if kwargs.get('skip_memory') is True and kwargs.get('parent_session_id'):
            # The background review fork (background_review.py:1299): same toolsets, memory re-bound by the caller.
            self.model, self.session_id, self.tools = kwargs['model'], kwargs['parent_session_id'], []
            return
        assert kwargs['enabled_toolsets'] == ['mcp-parachute', 'memory', 'skills'], kwargs['enabled_toolsets']
        assert kwargs['skip_memory'] is False
        assert kwargs['skip_background_review'] is False
        assert kwargs['max_iterations'] == 11
        assert kwargs['run_budget_seconds'] == 90
        assert kwargs['max_tokens'] == 6144
        assert kwargs['skip_context_files']
        assert kwargs['fallback_model'] is None
        self.model = kwargs['model']
        self.session_id = 'sess-fixture'
        self.offered = OFFERED.get(self.model, OFFERED['fixture-model'])
        self.publish(self.offered)
        self._skip_mcp_refresh = False
        self.client = SimpleNamespace(max_retries=2)
        self._client_kwargs = {}
        self.session_input_tokens = 200
        self.session_output_tokens = 40
        self._memory_nudge_interval, self._skill_nudge_interval = 10, 15
        self._turns_since_memory = self._iters_since_skill = 0
        store = {'drift-store': StoreWithNewWriter, 'drift-no-batch': StoreWithoutBatch}.get(self.model, FakeMemoryStore)
        self._memory_manager = FakeMemoryManager()
        self._memory_store = store()
    def publish(self, names):
        self.tools = [{'type': 'function', 'function': {'name': n}} for n in names]
        self.valid_tool_names = set(names)
    def _interruptible_api_call(self, *args, **kwargs): return None
    def _interruptible_streaming_api_call(self, *args, **kwargs): return None
    def _repair_tool_call(self, name): return None
    def _execute_tool_calls(self, assistant_message, messages, effective_task_id, api_call_count=0):
        # Hermes' real routes: memory through agent/tool_executor.py, skill_manage through the tool registry.
        from agent.tool_executor import run_memory
        from tools.registry import registry
        for call in assistant_message.tool_calls:
            name, args = call.function.name, json.loads(call.function.arguments or '{}')
            if name == 'memory':
                self._turns_since_memory = 0
                messages.append({'role': 'tool', 'content': run_memory(self, args)})
            elif name == 'skill_manage':
                self._iters_since_skill = 0
                messages.append({'role': 'tool', 'content': registry.dispatch('skill_manage', args)})
            else:
                note('dispatched:' + name)
            self._iters_since_skill += 1
        messages.append({'role': 'tool', 'content': '[]'})
    def api_call(self):
        self._interruptible_api_call({'tools': self.tools} if self.tools else {})
    def call(self, name, args=None):
        return SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(args or {})))])
    def review(self, calls):
        # background_review._run_review_in_thread: a new AIAgent, the parent's store re-bound, the class dispatch.
        def target():
            fork = AIAgent(model=self.model, parent_session_id=self.session_id, skip_memory=True, enabled_toolsets=['mcp-parachute', 'memory', 'skills'])
            fork._memory_store = self._memory_store
            time.sleep(0.2)
            for name, args in calls:
                try: fork._execute_tool_calls(fork.call(name, args), [], 'review')
                except Exception as error: note('review-refused:' + name)
            note('review:done')
        threading.Thread(target=target, daemon=True, name='bg-review').start()
    def run_conversation(self, text, system_message):
        packet = json.loads(text)
        assert 'untrusted data' in system_message and '"card"' in system_message
        assert 'Before drafting, check memory and the relevant skills' in system_message and 'omni-scheduling' in system_message
        # Turn-start MCP refresh: Hermes rebuilds the list from its registry, undoing any filter.
        if not self._skip_mcp_refresh or self.model == 'refresh-ignores-flag':
            self.publish(self.offered)
        if self.model in EXPECTED:
            assert [t['function']['name'] for t in self.tools] == EXPECTED[self.model], self.tools
        mm, store = self._memory_manager, self._memory_store
        errors = []
        mm.on_turn_start(1, text)
        mm.build_system_prompt(); store.format_for_system_prompt('memory')
        mm.prefetch_all(text)
        # turn_context.py:740 — the memory nudge counts turns.
        review_memory = False
        if self._memory_nudge_interval > 0 and 'memory' in self.valid_tool_names:
            self._turns_since_memory += 1
            review_memory = self._turns_since_memory >= self._memory_nudge_interval
        if packet.get('sleep'): time.sleep(packet['sleep'])
        if packet.get('break_audit'): os.chmod(packet['break_audit'], 0o400)  # the audit log stops being writable mid-run
        for item in packet.get('tool_calls', []):
            name, args = (item, {}) if isinstance(item, str) else (item['name'], item.get('args') or {})
            try:
                self.api_call()
                # An unknown name goes to _repair_tool_call and is never dispatched.
                # 'raw:' skips that check, as a runtime without pre-dispatch validation would.
                if name.startswith('raw:'): name = name[4:]
                elif name not in self.valid_tool_names:
                    self._repair_tool_call(name)
                    errors.append('unknown tool ' + name)
                    continue
                self._execute_tool_calls(self.call(name, args), [], 'task')
            except Exception as error:  # Hermes: append "Error executing tool" and keep going
                errors.append(repr(error))
        try:
            self.api_call()
        except Exception as error:
            errors.append(repr(error))
        reply = json.dumps({'decisions': []})
        if packet.get('reply') == 'fenced': reply = 'Sure.\\n```json\\n' + reply + '\\n```\\nDone.'
        if packet.get('reply') == 'prose': reply = 'Use { braces "{" sparingly. Here is what I found: ' + reply + ' Let me know.'
        if packet.get('reply') == 'two': reply = reply + ' and also ' + json.dumps({'decisions': [{'sourceId': 'x'}]})
        mm.sync_all(text, reply)
        # turn_finalizer.py:781 — the skill nudge counts tool iterations; the review needs a nudge to fire.
        review_skills = self._skill_nudge_interval > 0 and self._iters_since_skill >= self._skill_nudge_interval
        if packet.get('review') is not None and (review_memory or review_skills):
            self.review([(c['name'], c.get('args') or {}) for c in packet['review']])
        return {'final_response': reply, 'errors': errors}
'''
RUNTIME = '''def resolve_runtime_provider(**kwargs):
    return {'api_key': 'synthetic-credential', 'provider': 'fixture-provider', 'base_url': 'http://127.0.0.1:1'}
'''
SKILLS = '''import json, os
from pathlib import Path
HERE = Path(os.path.dirname(os.path.abspath(__file__))).parent
SKILLS_DIR = HERE / 'home' / 'skills'
EXTERNAL = HERE / 'repo' / 'hermes' / 'skills'
def _skills_dir(): return SKILLS_DIR
def _resolve_skill_dir(name, category=None):
    return SKILLS_DIR / category / name if category else SKILLS_DIR / name
def _find_skill(name):
    for root in (SKILLS_DIR, EXTERNAL):
        for md in root.rglob('SKILL.md'):
            if md.parent.name == name: return {'path': md.parent}
    return None
def skill_manage(action, name, content=None, category=None, file_path=None, file_content=None, old_string=None,
                 new_string=None, replace_all=False, absorbed_into=None, task_id=None, session_id=None):
    if action == 'create':
        if _find_skill(name): return json.dumps({'success': False, 'error': 'exists'})
        folder = _resolve_skill_dir(name, category); folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text(content)
        return json.dumps({'success': True})
    found = _find_skill(name)
    if not found: return json.dumps({'success': False, 'error': 'not found'})
    md = found['path'] / 'SKILL.md'
    if action == 'patch':
        md.write_text(md.read_text().replace(old_string, new_string)); return json.dumps({'success': True})
    if action == 'delete':
        md.unlink(); return json.dumps({'success': True})
    return json.dumps({'success': False, 'error': 'unknown action'})
from tools.registry import registry
if os.environ.get('FIXTURE_DIRECT_REGISTRATION'):
    registry.register('skill_manage', skill_manage)  # a Hermes that captures the function at import
else:
    registry.register('skill_manage', lambda args, **kw: skill_manage(**args))  # Hermes today: global looked up per call
'''
REGISTRY = '''class Registry:
    def __init__(self): self.handlers = {}
    def register(self, name, handler): self.handlers[name] = handler
    def dispatch(self, name, args, **kw): return self.handlers[name](args, **kw)
registry = Registry()
'''
MEMORY_TOOL = '''import json
def memory_tool(action=None, target='memory', content=None, old_text=None, new_text=None, operations=None, store=None):
    if store is None: return json.dumps({'success': False, 'error': 'no store'})
    if operations: return json.dumps(store.apply_batch(target, operations))
    if action == 'add': return json.dumps(store.add(target, content))
    if action == 'replace': return json.dumps(store.replace(target, old_text, content))
    if action == 'remove': return json.dumps(store.remove(target, old_text))
    return json.dumps({'success': False})
'''
EXECUTOR = '''import os
def run_memory(agent, next_args):
    from tools.memory_tool import memory_tool as _memory_tool
    return _memory_tool(action=next_args.get('action', 'add'), target=next_args.get('target', 'memory'), content=next_args.get('content'),
                        old_text=next_args.get('old_text'), operations=next_args.get('operations'),
                        store=agent._memory_store)
'''
PREPROCESSING = '''import os
def load_skills_config():
    if os.environ.get('FIXTURE_SKILLS_CONFIG_BROKEN'): raise OSError('config unreadable')
    return {'template_vars': True, 'inline_shell': os.environ.get('FIXTURE_INLINE_SHELL') == '1'}
'''
CONFIG = '''import os
def load_config():
    if os.environ.get('FIXTURE_HERMES_CONFIG_BROKEN'): raise OSError('config unreadable')
    return {'memory': {'memory_char_limit': 8000, 'write_approval': os.environ.get('FIXTURE_WRITE_APPROVAL') == '1'}}
'''
SECRET = 'fixtureSecretValue0123456789abcdef'


class CardsAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        (self.folder / 'run_agent.py').write_text(AGENT)
        (self.folder / 'hermes_cli').mkdir()
        (self.folder / 'hermes_cli' / '__init__.py').write_text('')
        (self.folder / 'hermes_cli' / 'runtime_provider.py').write_text(RUNTIME)
        (self.folder / 'hermes_cli' / 'config.py').write_text(CONFIG)
        (self.folder / 'tools').mkdir()
        (self.folder / 'tools' / '__init__.py').write_text('')
        (self.folder / 'tools' / 'skill_manager_tool.py').write_text(SKILLS)
        (self.folder / 'tools' / 'registry.py').write_text(REGISTRY)
        (self.folder / 'tools' / 'memory_tool.py').write_text(MEMORY_TOOL)
        (self.folder / 'agent').mkdir()
        (self.folder / 'agent' / '__init__.py').write_text('')
        (self.folder / 'agent' / 'tool_executor.py').write_text(EXECUTOR)
        (self.folder / 'agent' / 'skill_preprocessing.py').write_text(PREPROCESSING)
        self.local = self.folder / 'home' / 'skills'
        self.repo_skill = self.folder / 'repo' / 'hermes' / 'skills' / 'omni-scheduling'
        (self.local / 'notes').mkdir(parents=True)
        (self.local / 'notes' / 'SKILL.md').write_text('notes v1')
        self.repo_skill.mkdir(parents=True)
        (self.repo_skill / 'SKILL.md').write_text('repo scheduling rules')
        self.audit = self.folder / 'state' / 'learning-audit.jsonl'
        self.reasoners = []

    def tearDown(self):
        for reasoner in self.reasoners: reasoner.reap(0)
        self.temp.cleanup()

    def reasoner(self, model='fixture-model'):
        reasoner = HermesReasoner(self.folder, model, hermes_python=sys.executable, timeout=20, audit_path=self.audit)
        self.reasoners.append(reasoner)
        return reasoner

    def reason(self, model='fixture-model', **packet):
        return self.reasoner(model).reason(dict({'mode': 'cards', 'events': []}, **packet))

    def refused(self, model='fixture-model', **packet):
        with self.assertRaises(RuntimeError) as caught:
            self.reason(model, **packet)
        return str(caught.exception)

    def memory_log(self):
        path = self.folder / 'memory.log'
        return path.read_text().split() if path.exists() else []

    def all_audit_lines(self):
        return [json.loads(l) for l in self.audit.read_text().splitlines()] if self.audit.exists() else []

    def audit_lines(self):
        return [l for l in self.all_audit_lines() if l['phase'] == 'done']

    # --- the run's configuration

    def test_the_learning_tools_and_the_background_review_are_enabled(self):
        # The fake AIAgent asserts enabled_toolsets ['mcp-parachute', 'memory', 'skills'], skip_background_review False,
        # skip_memory False and 11 model turns; the system prompt carries the learning instruction.
        result = self.reason('double-underscore')
        self.assertEqual(result['usage']['toolsAvailable'], 3)
        self.assertEqual(result['usage']['modelTurnsLimit'], 11)
        self.assertEqual(hermes_adapter.CARD_TOOLSETS, ['mcp-parachute', 'memory', 'skills'])
        self.assertEqual(LEARNING_TOOLS, frozenset(LEARN))
        system = hermes_adapter.cards_system()
        self.assertIn('you may `skill_view` `omni-scheduling` for its rules, but never run its commands', system)
        self.assertIn('the engine places, confirms and releases the holds itself', system)
        self.assertIn('record durable lessons with `memory` or `skill_manage`', hermes_adapter.cards_system())

    def test_the_review_nudges_fire_every_run(self):
        self.assertEqual(hermes_adapter.REVIEW_NUDGE_INTERVAL, 1)
        self.reason(review=[{'name': 'memory', 'args': {'content': 'Alex prefers Tuesday calls'}}])
        self.reasoners[-1].reap()
        self.assertIn('review:done', self.memory_log())

    # --- bounds and counting

    def test_reads_and_learning_calls_are_counted(self):
        result = self.reason(tool_calls=['mcp_parachute_query_notes', 'mcp_parachute_query_notes', 'skill_view'])
        self.assertEqual(result['usage']['toolReads'], 2)
        self.assertEqual(result['usage']['toolCalls'], 3)
        self.assertEqual(result['usage']['nativeCalls'], 4)
        self.assertIs(result['usage']['memory'], True)
        self.assertEqual(result['usage']['toolsAvailable'], 1)
        self.assertEqual(result['usage']['modelTurnsLimit'], 11)

    def test_the_tool_budget_is_ten_calls(self):
        self.assertEqual(self.reason(tool_calls=['mcp_parachute_query_notes'] * 10)['usage']['toolReads'], 10)
        self.assertIn('ToolPolicyError', self.refused(tool_calls=['mcp_parachute_query_notes'] * 11))
        self.assertIn('ToolPolicyError', self.refused(tool_calls=['skill_view'] * 11))

    def test_an_eleventh_double_underscore_call_fails_the_run(self):
        self.assertIn('ToolPolicyError', self.refused('double-underscore', tool_calls=['mcp__parachute__vault_info'] * 11))

    def test_the_run_is_hard_stopped_after_the_card_bound_plus_grace(self):
        with mock.patch.object(hermes_adapter, 'CARD_RUN_SECONDS', 1), mock.patch.object(hermes_adapter, 'CARD_GRACE_SECONDS', 1):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                self.reason(sleep=15)
            self.assertLess(time.monotonic() - started, 10)

    def test_legacy_keeps_its_own_timeout(self):
        reasoner = HermesReasoner(self.folder, 'fixture-model', hermes_python=sys.executable)
        self.assertEqual(reasoner.run_timeout({'events': []}), 180)
        self.assertEqual(reasoner.run_timeout({'mode': 'cards'}), hermes_adapter.CARD_RUN_SECONDS + hermes_adapter.CARD_GRACE_SECONDS)

    # --- tools: offered list and dispatch

    def test_the_vault_allowlist_is_both_spellings_of_three_read_tools_and_nothing_else(self):
        self.assertEqual(READ_ONLY_TOOLS, frozenset({
            'mcp_parachute_query_notes', 'mcp_parachute_find_path', 'mcp_parachute_vault_info',
            'mcp__parachute__query_notes', 'mcp__parachute__find_path', 'mcp__parachute__vault_info'}))
        self.assertEqual(hermes_adapter.CARD_TOOLS, READ_ONLY_TOOLS | frozenset(LEARN))

    def test_double_underscore_names_keep_the_three_read_tools_and_the_learning_tools(self):
        result = self.reason('double-underscore', tool_calls=['mcp__parachute__query_notes', 'mcp__parachute__find_path', 'mcp__parachute__vault_info'])
        self.assertEqual(result['usage']['toolReads'], 3)
        self.assertEqual(result['usage']['toolsAvailable'], 3)

    def test_turn_start_mcp_refresh_is_switched_off(self):
        # Without _skip_mcp_refresh the fake re-publishes write tools and the run would fail.
        self.assertEqual(self.reason('double-underscore')['usage']['toolsAvailable'], 3)

    def test_a_write_tool_offered_to_the_model_fails_the_run(self):
        self.assertIn('ToolPolicyError', self.refused('refresh-ignores-flag', tool_calls=['mcp_parachute_query_notes']))
        self.assertIn('ToolPolicyError', self.refused('refresh-ignores-flag'))

    def test_a_write_tool_call_is_refused_as_a_tool_error_and_counted(self):
        # I-4 (final review 2026-10-23): Hermes answers the model "does not exist"; the tick is not lost.
        result = self.reason(tool_calls=['mcp_parachute_update_note', 'mcp_parachute_query_notes'])
        self.assertEqual(result['usage']['refusedTools'], 1)
        self.assertEqual(result['decisions'], [])
        self.assertNotIn('dispatched:mcp_parachute_update_note', self.memory_log())

    def test_vault_write_terminal_web_and_messaging_calls_are_refused_never_dispatched(self):
        names = ('mcp__parachute__update_note', 'mcp__parachute__delete_note', 'terminal', 'write_file', 'web_search', 'send_message')
        result = self.reason('double-underscore', tool_calls=list(names))
        self.assertEqual(result['usage']['refusedTools'], len(names))
        for name in names:
            self.assertNotIn('dispatched:' + name, self.memory_log())

    def test_a_skill_command_after_skill_view_costs_a_tool_error_not_the_run(self):
        result = self.reason(tool_calls=[{'name': 'skill_view', 'args': {'name': 'omni-scheduling'}}, {'name': 'terminal', 'args': {'command': 'python3 scripts/manage_holds.py list'}}])
        self.assertEqual(result['usage']['refusedTools'], 1)
        self.assertNotIn('dispatched:terminal', self.memory_log())

    def test_an_outside_call_that_reaches_dispatch_still_fails_the_run(self):
        # A runtime that skipped its own name check (Hermes drift): the dispatch layer refuses the whole run.
        for name in ('raw:mcp_parachute_update_note', 'raw:terminal', 'raw:mcp_parachute_find_path'):
            self.assertIn('ToolPolicyError', self.refused(tool_calls=[name]), name)

    def test_memory_write_approval_on_or_an_unreadable_config_refuses_the_run(self):
        with mock.patch.dict(os.environ, {'FIXTURE_WRITE_APPROVAL': '1'}):
            self.assertIn('memory.write_approval is on', self.refused())
        with mock.patch.dict(os.environ, {'FIXTURE_HERMES_CONFIG_BROKEN': '1'}):
            self.assertIn('cannot confirm memory.write_approval is off', self.refused())

    def test_no_vault_tools_still_produces_cards_and_warns(self):
        with self.assertLogs('proactive_intelligence.hermes_adapter', level='WARNING') as logs:
            result = self.reason('no-vault')
        self.assertEqual(result['decisions'], [])
        self.assertEqual(result['usage']['toolsAvailable'], 0)
        self.assertEqual(result['usage']['toolReads'], 0)
        self.assertIn('without vault tools', '\n'.join(logs.output))

    def test_no_vault_tools_and_a_terminal_call_is_refused_and_counted(self):
        self.assertEqual(self.reason('no-vault', tool_calls=['terminal'])['usage']['refusedTools'], 1)
        self.assertNotIn('dispatched:terminal', self.memory_log())

    # --- memory: recall and audited writes

    def test_a_memory_write_lands_and_is_audited(self):
        result = self.reason(tool_calls=[{'name': 'memory', 'args': {'action': 'add', 'target': 'user', 'content': 'Alex takes calls after 10'}}])
        log = self.memory_log()
        self.assertIn('recall:prefetch_all', log)
        self.assertIn('write:store.add:user', log)
        self.assertIn('provider:sync_all', log, 'memory saves are no longer patched to no-ops')
        self.assertEqual(result['usage']['learning']['memoryWrites'], 1)
        [line] = self.audit_lines()
        self.assertEqual({k: line[k] for k in ('session', 'actor', 'tool', 'action', 'target', 'ok')},
                         {'session': 'sess-fixture', 'actor': 'proactive', 'tool': 'memory', 'action': 'add', 'target': 'USER.md', 'ok': True})
        self.assertLessEqual(len(line['summary']), 200)
        self.assertRegex(line['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(stat.S_IMODE(os.stat(self.audit).st_mode), 0o600)

    def test_the_audit_summary_never_carries_a_secret_and_is_capped(self):
        content = 'remember token ' + SECRET + ' ' + 'x' * 400
        with mock.patch.dict(os.environ, {'FIXTURE_API_KEY': SECRET}):
            self.reason(tool_calls=[{'name': 'memory', 'args': {'action': 'add', 'content': content}}])
        [line] = self.audit_lines()
        self.assertNotIn(SECRET, json.dumps(line))
        self.assertLessEqual(len(line['summary']), 200)
        self.assertEqual(line['target'], 'MEMORY.md')

    def test_the_audit_is_append_only(self):
        self.reason(tool_calls=[{'name': 'memory', 'args': {'content': 'first'}}])
        self.reason(tool_calls=[{'name': 'memory', 'args': {'action': 'remove', 'old_text': 'first'}}])
        self.assertEqual([l['action'] for l in self.audit_lines()], ['add', 'remove'])

    def test_an_unknown_memory_store_method_refuses_cards_mode(self):
        for model in ('drift-store', 'drift-no-batch'):
            self.assertIn('MemoryGuardError', self.refused(model), model)

    # --- skills: local writes audited, the repo's skills untouched

    def test_a_local_skill_write_lands_and_is_audited(self):
        result = self.reason(tool_calls=[{'name': 'skill_manage', 'args': {'action': 'create', 'name': 'scheduling-lessons', 'content': 'Prefer mornings'}},
                                         {'name': 'skill_manage', 'args': {'action': 'patch', 'name': 'notes', 'old_string': 'v1', 'new_string': 'v2'}}])
        self.assertEqual((self.local / 'scheduling-lessons' / 'SKILL.md').read_text(), 'Prefer mornings')
        self.assertEqual((self.local / 'notes' / 'SKILL.md').read_text(), 'notes v2')
        self.assertEqual(result['usage']['learning']['skillWrites'], 2)
        self.assertEqual([(l['tool'], l['action'], l['target'], l['ok']) for l in self.audit_lines()],
                         [('skill_manage', 'create', 'scheduling-lessons', True), ('skill_manage', 'patch', 'notes', True)])

    def test_a_repo_skill_in_external_dirs_is_never_written(self):
        result = self.reason(tool_calls=[{'name': 'skill_manage', 'args': {'action': 'patch', 'name': 'omni-scheduling', 'old_string': 'repo', 'new_string': 'hacked'}},
                                         {'name': 'skill_manage', 'args': {'action': 'delete', 'name': 'omni-scheduling'}}])
        self.assertEqual((self.repo_skill / 'SKILL.md').read_text(), 'repo scheduling rules')
        self.assertEqual(result['usage']['learning']['refused'], 2)
        lines = self.audit_lines()
        self.assertEqual([(l['target'], l['ok'], l['refused']) for l in lines], [('omni-scheduling', False, 'outside ~/.hermes/skills')] * 2)

    def test_a_skill_reached_through_a_symlink_or_a_path_is_refused(self):
        outside = self.folder / 'elsewhere' / 'linked'
        outside.mkdir(parents=True)
        (outside / 'SKILL.md').write_text('linked v1')
        (self.local / 'linked').symlink_to(outside)
        self.reason(tool_calls=[{'name': 'skill_manage', 'args': {'action': 'patch', 'name': 'linked', 'old_string': 'v1', 'new_string': 'v2'}},
                                {'name': 'skill_manage', 'args': {'action': 'create', 'name': '../../escape', 'content': 'x'}}])
        self.assertEqual((outside / 'SKILL.md').read_text(), 'linked v1')
        self.assertFalse((self.folder / 'escape').exists())
        self.assertEqual([l['ok'] for l in self.audit_lines()], [False, False])

    # --- fix round 1: the guard is on the real call path, the audit comes first, no inline shell

    def test_an_intent_line_is_written_before_the_write_and_a_done_line_after(self):
        with mock.patch.dict(os.environ, {'FIXTURE_AUDIT_PATH': str(self.audit)}):
            self.reason(tool_calls=[{'name': 'memory', 'args': {'action': 'add', 'content': 'Prefers mornings'}},
                                    {'name': 'skill_manage', 'args': {'action': 'create', 'name': 'lessons', 'content': 'x'}}])
        self.assertIn('audit-lines-at-write:1', self.memory_log())
        lines = self.all_audit_lines()
        self.assertEqual([(l['tool'], l['phase'], l['ok']) for l in lines],
                         [('memory', 'intent', None), ('memory', 'done', True), ('skill_manage', 'intent', None), ('skill_manage', 'done', True)])

    def test_a_write_whose_intent_line_cannot_be_appended_is_refused_and_fails_the_run(self):
        self.audit.parent.mkdir(parents=True, exist_ok=True)
        self.audit.write_text('')
        message = self.refused(break_audit=str(self.audit),
                               tool_calls=[{'name': 'memory', 'args': {'action': 'add', 'content': 'unlogged lesson'}},
                                           {'name': 'skill_manage', 'args': {'action': 'patch', 'name': 'notes', 'old_string': 'v1', 'new_string': 'v2'}}])
        self.assertIn('audit-unavailable', message)
        self.assertNotIn('write:store.add:memory', self.memory_log())
        self.assertEqual((self.local / 'notes' / 'SKILL.md').read_text(), 'notes v1')

    def test_an_unwritable_or_symlinked_audit_log_refuses_the_run_before_it_starts(self):
        self.audit.parent.mkdir(parents=True, exist_ok=True)
        elsewhere = self.folder / 'elsewhere.jsonl'
        elsewhere.write_text('')
        self.audit.symlink_to(elsewhere)
        self.assertIn('learning audit log is not writable', self.refused(tool_calls=[{'name': 'memory', 'args': {'content': 'x'}}]))
        self.assertEqual(elsewhere.read_text(), '')
        self.assertNotIn('write:store.add:memory', self.memory_log())

    def test_a_skill_manage_registered_directly_bypassing_the_guard_refuses_the_run(self):
        with mock.patch.dict(os.environ, {'FIXTURE_DIRECT_REGISTRATION': '1'}):
            message = self.refused(tool_calls=[{'name': 'skill_manage', 'args': {'action': 'patch', 'name': 'omni-scheduling', 'old_string': 'repo', 'new_string': 'x'}}])
        self.assertIn('MemoryGuardError', message)
        self.assertIn('skill_manage is not routed through the guard', message)
        self.assertEqual((self.repo_skill / 'SKILL.md').read_text(), 'repo scheduling rules')

    def test_a_memory_tool_that_no_longer_uses_the_agent_store_refuses_the_run(self):
        (self.folder / 'agent' / 'tool_executor.py').write_text(
            'def run_memory(agent, next_args):\n'
            '    from tools.memory_tool import memory_tool\n'
            '    return memory_tool(action="add", content=next_args.get("content"), store=load_on_disk_store())\n')
        message = self.refused(tool_calls=[{'name': 'memory', 'args': {'content': 'x'}}])
        self.assertIn('the memory tool no longer passes agent._memory_store', message)

    def test_the_probes_leave_no_audit_line_and_no_write(self):
        self.reason()
        self.assertEqual(self.all_audit_lines(), [])
        self.assertEqual([e for e in self.memory_log() if e.startswith('write:')], [])

    def test_inline_shell_on_or_an_unreadable_skills_config_refuses_the_run(self):
        with mock.patch.dict(os.environ, {'FIXTURE_INLINE_SHELL': '1'}):
            self.assertIn('skills.inline_shell is on', self.refused())
        with mock.patch.dict(os.environ, {'FIXTURE_SKILLS_CONFIG_BROKEN': '1'}):
            self.assertIn('cannot confirm skills.inline_shell is off', self.refused())

    # --- the background review

    def test_background_review_writes_are_audited_and_confined(self):
        result = self.reason(review=[{'name': 'memory', 'args': {'content': 'Alex declines Friday calls'}},
                                     {'name': 'skill_manage', 'args': {'action': 'patch', 'name': 'omni-scheduling', 'old_string': 'repo', 'new_string': 'x'}},
                                     {'name': 'terminal', 'args': {'command': 'id'}},
                                     {'name': 'mcp__parachute__update_note', 'args': {}}])
        self.assertEqual(result['usage']['learning']['backgroundReview'], 'running')
        self.reasoners[-1].reap()
        log = self.memory_log()
        self.assertIn('review:done', log)
        self.assertIn('write:store.add:memory', log)
        self.assertNotIn('dispatched:terminal', log)
        self.assertNotIn('dispatched:mcp__parachute__update_note', log)
        self.assertEqual((self.repo_skill / 'SKILL.md').read_text(), 'repo scheduling rules')
        lines = self.audit_lines()
        self.assertTrue(all(l['actor'] == 'background-review' for l in lines), lines)
        self.assertEqual([(l['tool'], l['ok']) for l in lines],
                         [('memory', True), ('skill_manage', False), ('terminal', False), ('mcp__parachute__update_note', False)])

    def test_the_worker_waits_for_the_review_before_exiting(self):
        reasoner = self.reasoner()
        reasoner.reason({'mode': 'cards', 'events': [], 'review': [{'name': 'memory', 'args': {'content': 'late lesson'}}]})
        self.assertEqual(len(reasoner._reviews), 1)
        reasoner.reap()
        self.assertEqual(reasoner._reviews, [])
        self.assertIn('review:done', self.memory_log())

    # --- reply parsing end to end

    def test_a_reply_wrapped_in_prose_or_a_fence_is_accepted(self):
        for reply in ('fenced', 'prose'):
            self.assertEqual(self.reason(reply=reply)['decisions'], [], reply)

    def test_a_reply_with_two_different_objects_fails(self):
        with self.assertRaises(RuntimeError):
            self.reason(reply='two')


class LearningApiTests(unittest.TestCase):
    """The learning guards fail closed on a Hermes whose memory store or skill module has changed shape."""

    def store_agent(self, **extra):
        attrs = {n: (lambda self, *a, **k: {'success': True}) for n in hermes_adapter._MEMORY_STORE_WRITES + hermes_adapter._MEMORY_STORE_READS}
        attrs.update(extra)
        agent = type('Agent', (), {})()
        agent._memory_store = type('Store', (), attrs)()
        return agent

    def skills(self, **drop):
        module = type('M', (), {n: staticmethod(lambda *a, **k: None) for n in hermes_adapter._SKILL_GUARD_API})
        for n in drop: setattr(module, n, None)
        return module

    def test_the_known_api_passes(self):
        hermes_adapter._check_learning_api(self.store_agent(), self.skills())

    def test_hermes_v023_store_passes_and_its_patch_is_audited(self):
        # v0.23 added patch (a writer) and two dry-run resolvers (readers). The run goes ahead and patch is wrapped.
        agent = self.store_agent(patch=lambda self, *a, **k: {'success': True},
                                 resolve_entry=lambda self, *a, **k: {'success': True},
                                 resolve_batch_entries=lambda self, *a, **k: {'success': True})
        hermes_adapter._check_learning_api(agent, self.skills())
        counts = {'memory': 0, 'failed': 0, 'refused': 0}
        with tempfile.TemporaryDirectory() as d:
            audit = Path(d) / 'audit.jsonl'
            hermes_adapter._audit_memory_store(agent, audit, counts)
            store = agent._memory_store
            self.assertTrue(getattr(store.patch, '__omni_audited__', False))
            store.patch('memory', 'tea', 'coffee', 'the whole old entry about tea')
            store.replace('memory', 'tea', 'new whole entry', 'the whole old entry about tea')
            lines = [json.loads(l) for l in audit.read_text().splitlines()]
        self.assertEqual(counts['memory'], 2)
        self.assertEqual([l['action'] for l in lines if l['phase'] == 'done'], ['patch', 'replace'])
        summaries = [l['summary'] for l in lines if l['phase'] == 'done']
        self.assertIn('coffee', summaries[0])
        self.assertIn('new whole entry', summaries[1])
        self.assertFalse(any('whole old entry' in x for x in summaries),
                         'the audit records the new text, not the matched entry passed after it')

    def test_memory_dispatch_source_reads_the_v023_inline_executor(self):
        # v0.23 moved `store=agent._memory_store` from agent/tool_executor.py to agent/inline_tool_executors.py.
        import types as _t
        old = _t.ModuleType('agent.tool_executor')
        new = _t.ModuleType('agent.inline_tool_executors')
        sources = {old: 'from agent.inline_tool_executors import _memory\n', new: 'store=agent._memory_store,\n'}
        with mock.patch('importlib.import_module', side_effect=lambda n: {'agent.tool_executor': old, 'agent.inline_tool_executors': new}[n]), \
                mock.patch('inspect.getsource', side_effect=lambda m: sources[m]):
            self.assertIn('store=agent._memory_store', hermes_adapter._memory_dispatch_source())
        with mock.patch('importlib.import_module', side_effect=ImportError):
            with self.assertRaises(ImportError):
                hermes_adapter._memory_dispatch_source()

    def test_an_unknown_store_method_refuses(self):
        with self.assertRaises(hermes_adapter.MemoryGuardError):
            hermes_adapter._check_learning_api(self.store_agent(consolidate=lambda self: None), self.skills())

    def test_a_missing_skill_helper_or_module_refuses(self):
        with self.assertRaises(hermes_adapter.MemoryGuardError):
            hermes_adapter._check_learning_api(self.store_agent(), self.skills(_find_skill=True))
        with self.assertRaises(hermes_adapter.MemoryGuardError):
            hermes_adapter._check_learning_api(self.store_agent(), None)


class LearningAuditTests(unittest.TestCase):
    def test_summary_is_scrubbed_before_and_after_the_cut(self):
        from proactive_intelligence import learning_audit
        scrub = lambda text: text.replace(SECRET, '[redacted]')
        out = learning_audit.summarize('a ' * 90 + SECRET + ' tail', scrub)
        self.assertNotIn(SECRET[:12], out)
        self.assertLessEqual(len(out), 200)

    def test_without_a_scrubber_the_entry_is_metadata_only(self):
        from proactive_intelligence import learning_audit
        with mock.patch.object(learning_audit, '_scrubber', lambda: None):
            e = learning_audit.entry(session='s', tool='memory', action='add', target='MEMORY.md', ok=True, text='secret-ish words', size=16)
        self.assertEqual(e['summary'], 'add MEMORY.md 16 chars')

    def test_inline_shell_values(self):
        for value in (False, None, 'false', 'off', 0):
            hermes_adapter._check_inline_shell(lambda: {'inline_shell': value})
        for value in (True, 'true', 'yes', 1, 'on'):
            with self.assertRaises(hermes_adapter.MemoryGuardError):
                hermes_adapter._check_inline_shell(lambda: {'inline_shell': value})
        with self.assertRaises(hermes_adapter.MemoryGuardError):
            hermes_adapter._check_inline_shell(lambda: 'not a mapping')

    def test_the_omni_bridge_scrubber_loads_from_the_repo(self):
        from proactive_intelligence import learning_audit
        self.assertTrue(callable(learning_audit._scrubber()))

    def test_append_creates_a_0600_file_and_appends(self):
        from proactive_intelligence import learning_audit
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state' / 'learning-audit.jsonl'
            self.assertTrue(learning_audit.append(path, {'n': 1}))
            os.chmod(path, 0o644)
            self.assertTrue(learning_audit.append(path, {'n': 2}))
            self.assertEqual([json.loads(l)['n'] for l in path.read_text().splitlines()], [1, 2])
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class ExtractJsonTests(unittest.TestCase):
    def test_fenced_reply(self):
        self.assertEqual(extract_json_object('```json\n{"decisions": []}\n```'), {'decisions': []})

    def test_prose_before_and_after(self):
        self.assertEqual(extract_json_object('Here you go: {"decisions": [{"b": [1, 2]}]} hope it helps}'), {'decisions': [{'b': [1, 2]}]})

    def test_two_different_objects_fail(self):
        with self.assertRaisesRegex(ValueError, 'more than one'):
            extract_json_object('{"decisions": [1]} or maybe {"decisions": [2]}')

    def test_two_identical_objects_agree(self):
        self.assertEqual(extract_json_object('{"decisions": []}\nagain: {"decisions": []}'), {'decisions': []})

    def test_braces_and_escapes_inside_strings(self):
        text = 'Result {"decisions": [], "t": "a } b { c", "q": "say \\"}{\\" now", "s": "back\\\\"} end'
        self.assertEqual(extract_json_object(text), {'decisions': [], 't': 'a } b { c', 'q': 'say "}{" now', 's': 'back\\'})

    def test_a_stray_brace_or_quoted_brace_in_prose_is_skipped(self):
        for text in ('Use { then {"decisions": []}', 'He said "{" then {"decisions": []}', 'Use {this} form: {"decisions": []}',
                     'Example {"a": 1} then the answer {"decisions": []}'):
            self.assertEqual(extract_json_object(text), {'decisions': []}, text)

    def test_no_object_fails(self):
        for text in ('no json here', '[1, 2]', '{"decisions": []', '{"a": 1}', ''):
            with self.assertRaises(ValueError, msg=text):
                extract_json_object(text)

    def test_candidates_are_capped_at_twenty(self):
        with self.assertRaisesRegex(ValueError, 'no JSON object'):
            extract_json_object('{x} ' * 25 + '{"decisions": []}')
        self.assertEqual(extract_json_object('{x} ' * 15 + '{"decisions": []}'), {'decisions': []})

    def test_a_200k_reply_parses_in_linear_time(self):
        body = json.dumps({'decisions': [{'body': 'x{y}"z' * 10, 'n': i} for i in range(2000)]})
        text = 'Here is { what "{" I found. ' + 'prose without braces ' * 2000 + body + ' trailing {' * 10
        text = text + ' ' * max(0, 200000 - len(text))
        self.assertGreaterEqual(len(text), 200000)
        started = time.process_time()
        self.assertEqual(len(extract_json_object(text)['decisions']), 2000)
        self.assertLess(time.process_time() - started, 0.5)
        for flood in ('{' * 200000, '{"decisions": "' + 'x' * 200000, ('{"a": "' + 'x' * 10000) * 20):
            started = time.process_time()
            with self.assertRaises(ValueError):
                extract_json_object(flood)
            self.assertLess(time.process_time() - started, 0.5)


class RunnerCountsToolessRunsTests(unittest.TestCase):
    """A cards run that had no vault tools is recorded so health can say 'cards ran without vault tools'."""
    def setUp(self):
        from proactive_intelligence.state import State
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / 'state.db')

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def tick(self, tools_available, n, error=None):
        from proactive_intelligence.runner import Runner
        now = 1792080000000 + n * 3600000
        event = dict(id='a%d' % n, revision='r1', kind='unprocessed', entity='a%d' % n, occurred_at=now, content='Could you review the budget sheet?',
                     url='prism://note/a%d' % n, channel='email', participants=['sam@example.com'])
        self.state.ingest('mail', str(n), [event], now=now, coalesce_ms=0)

        class Reasoner:
            def reason(self, packet):
                if error: raise error
                e = packet['events'][0]
                return {'decisions': [dict(sourceId=e['id'], sourceRevision=e['revision'], action='ignore', title='Nothing to do', summary='A note.', body='No action.',
                                           evidence=[{'sourceId': e['id'], 'revision': e['revision']}])],
                        'usage': {'memory': True, 'toolReads': 0, 'toolsAvailable': tools_available}}
        return Runner(self.state, Reasoner(), clock=lambda: now, cards=True).tick()

    def test_runs_without_vault_tools_are_counted_and_still_decide(self):
        receipt = self.tick(0, 1)
        self.assertEqual(len(receipt['decisions']), 1)
        self.assertEqual(receipt['vaultTools'], 'unavailable')
        self.tick(3, 2)
        self.tick(0, 3)
        self.assertEqual(self.state.cards_runs_without_vault_tools(), 2)
        results = [json.loads(r[0]).get('vaultTools') for r in self.state.db.execute("SELECT result FROM runs WHERE status='done' ORDER BY started")]
        self.assertEqual(results, ['unavailable', 'available', 'unavailable'])

    def test_a_refused_or_timed_out_run_fails_cleanly_and_names_why(self):
        for n, error, refusal in ((1, RuntimeError('MemoryGuardError at hermes_adapter.py:120'), 'MemoryGuardError'),
                                  (2, RuntimeError('ToolPolicyError at hermes_adapter.py:240'), 'ToolPolicyError'),
                                  (3, TimeoutError('Hermes proactive run timed out'), 'timeout')):
            with self.assertRaises(type(error)):
                self.tick(1, n, error)
            row = self.state.db.execute("SELECT status, result FROM runs ORDER BY started DESC LIMIT 1").fetchone()
            self.assertEqual(row[0], 'failed')
            self.assertEqual(json.loads(row[1])['refusal'], refusal)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_each_refusal_is_recorded_with_its_reason_for_health(self):
        cases = ((RuntimeError('MemoryGuardError at h.py:1: learning audit guard: Hermes memory/skill API changed: MemoryStore lacks add'), 'drift'),
                 (RuntimeError('MemoryGuardError at h.py:1: learning guard not in the call path: skill_manage is not routed'), 'drift'),
                 (RuntimeError('MemoryGuardError at h.py:1: skills.inline_shell is on: proactive runs refuse'), 'inline_shell'),
                 (RuntimeError('MemoryGuardError at h.py:1: memory.write_approval is on: proactive runs refuse'), 'write_approval'),
                 (RuntimeError('MemoryGuardError at h.py:1: learning audit log is not writable: learning-audit.jsonl'), 'audit-unavailable'),
                 (RuntimeError('ToolPolicyError at h.py:1: understanding step refused: audit-unavailable'), 'audit-unavailable'),
                 (RuntimeError('ToolPolicyError at h.py:1: understanding step called a tool outside the cards allowlist'), 'tool-outside-allowlist'),
                 (RuntimeError('ToolPolicyError at h.py:1: understanding step offered a tool outside the cards allowlist'), 'tool-outside-allowlist'),
                 (RuntimeError('ToolPolicyError at h.py:1: understanding step tool budget exceeded'), 'other'),
                 (TimeoutError('Hermes proactive run timed out'), 'timeout'),
                 (ValueError('bounded output exceeded'), 'other'))
        for n, (error, reason) in enumerate(cases, 1):
            with self.assertRaises(type(error)):
                self.tick(1, n, error)
            row = self.state.db.execute("SELECT result FROM runs ORDER BY started DESC LIMIT 1").fetchone()
            self.assertEqual(json.loads(row[0])['reason'], reason, error)
        got = self.state.run_failures()
        self.assertEqual(got, {'drift': 2, 'inline_shell': 1, 'write_approval': 1, 'audit-unavailable': 2, 'tool-outside-allowlist': 2,
                               'timeout': 1, 'other': 2})

    def test_refused_tool_calls_in_a_finished_run_count_for_health(self):
        self.state.db.execute("INSERT INTO runs(id,day,bucket,status,started,until_at,usage,result) VALUES('x','d','ordinary','done',1,2,?,'{}')",
                              (json.dumps({'refusedTools': 2}),))
        self.state.db.execute("INSERT INTO runs(id,day,bucket,status,started,until_at,usage,result) VALUES('y','d','ordinary','unknown',1,2,NULL,NULL)")
        self.assertEqual(self.state.run_failures(), {'tool-outside-allowlist': 2, 'lost': 1})


class VaultToolDiscoveryTests(unittest.TestCase):
    def test_discovery_runs_and_reports_done(self):
        calls = []
        self.assertTrue(hermes_adapter._discover_vault_tools({}, discover=lambda: calls.append(1), seconds=2))
        self.assertEqual(calls, [1])

    def test_a_failing_discovery_never_raises(self):
        def boom(): raise RuntimeError('mcp down')
        self.assertFalse(hermes_adapter._discover_vault_tools({}, discover=boom, seconds=2))

    def test_a_hanging_discovery_is_abandoned_after_the_bound(self):
        import threading
        gate = threading.Event()
        self.assertFalse(hermes_adapter._discover_vault_tools({}, discover=gate.wait, seconds=0.05))
        gate.set()


class ToolSearchOffTests(unittest.TestCase):
    def test_tool_search_is_turned_off_for_the_worker(self):
        class Config:
            def __init__(self, enabled): self.enabled = enabled
            @classmethod
            def from_raw(cls, raw): return cls((raw or {}).get('enabled', 'auto'))
        module = type('M', (), {'ToolSearchConfig': Config, 'load_config': staticmethod(lambda: Config('auto')),
                                'load_config_readonly': staticmethod(lambda: Config('auto'))})
        self.assertTrue(hermes_adapter._expose_tools_directly(module))
        self.assertEqual(module.load_config().enabled, 'off')
        self.assertEqual(module.load_config_readonly().enabled, 'off')

    def test_an_older_hermes_without_tool_search_is_left_alone(self):
        self.assertFalse(hermes_adapter._expose_tools_directly(type('M', (), {})))


class RunnerBudgetsTheRealPromptTests(unittest.TestCase):
    """2026-10-26: cards runs failed with 'bounded input packet plus system exceeded' because the runner trimmed
    evidence against SYSTEM_CARDS while the adapter sends SYSTEM_CARDS plus the learning prompt."""
    def test_a_cards_packet_is_trimmed_to_what_the_adapter_accepts(self):
        from proactive_intelligence.runner import Runner
        from proactive_intelligence.state import State
        from proactive_intelligence.hermes_adapter import cards_system
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp) / 'state.db')
            now = 1792080000000
            event = dict(id='a1', revision='r1', kind='unprocessed', entity='a1', occurred_at=now, content='Could you review the budget sheet?',
                         url='prism://note/a1', channel='email', participants=['sam@example.com'])
            state.ingest('mail', '1', [event], now=now, coalesce_ms=0)
            sizes = []

            class Assembler:
                def assemble(self, events):
                    return {'evidence': [{'id': 'e%d' % i, 'revision': 'r', 'content': 'x' * 900} for i in range(40)],
                            'health': 'healthy', 'toolReads': 0}

            class Reasoner:
                def reason(self, packet):
                    sizes.append(len(cards_system()) + len(json.dumps(packet)))
                    e = packet['events'][0]
                    return {'decisions': [dict(sourceId=e['id'], sourceRevision=e['revision'], action='ignore', title='Nothing to do',
                                               summary='A note.', body='No action.', evidence=[{'sourceId': e['id'], 'revision': e['revision']}])]}
            Runner(state, Reasoner(), clock=lambda: now, cards=True, context_assembler=Assembler()).tick()
            state.db.close()
        self.assertEqual(len(sizes), 1)
        self.assertLessEqual(sizes[0], 24000)


if __name__ == '__main__':
    unittest.main()
