"""Offline native-runtime contract fixture; no provider network requests."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from proactive_intelligence.hermes_adapter import HermesReasoner

AGENT = '''import json
from types import SimpleNamespace
class AIAgent:
    def __init__(self, **kwargs):
        assert kwargs['api_key'] == 'synthetic-credential'
        assert kwargs['provider'] == 'fixture-provider'
        assert kwargs['enabled_toolsets'] == []
        assert kwargs['max_iterations'] == 1
        assert kwargs['skip_context_files'] and kwargs['skip_memory'] and kwargs['skip_background_review']
        assert kwargs['fallback_model'] is None
        self.tools = ['unexpected'] if kwargs['model']=='tools-enabled' else []
        self.client=SimpleNamespace(max_retries=2)
        self._client_kwargs={}
        self.session_input_tokens=120
        self.session_output_tokens=20
    def _interruptible_api_call(self, *args, **kwargs): return None
    def _interruptible_streaming_api_call(self, *args, **kwargs): return None
    def run_conversation(self, text, system_message):
        assert self.compression_enabled is False
        assert self.client.max_retries==0
        assert self._has_pending_fallback() is False
        assert self._try_activate_fallback() is False
        packet=json.loads(text)
        if packet.get('stream_entry'): self._interruptible_streaming_api_call({}, on_first_delta=None)
        else: self._interruptible_api_call({})
        if packet.get('extra_call'): self._interruptible_streaming_api_call({})
        if packet.get('extra_summary'): self._handle_max_iterations([],1)
        reply=json.dumps({'decisions':[]})
        if packet.get('reply')=='fenced': reply='```json\\n'+reply+'\\n```'
        if packet.get('reply')=='prose': reply='Here are the cards: '+reply
        return {'final_response':reply}
'''
RUNTIME = '''def resolve_runtime_provider(**kwargs):
    assert kwargs['target_model'] in ('fixture-model','tools-enabled')
    return {'api_key':'synthetic-credential','provider':'fixture-provider','base_url':'http://127.0.0.1:1'}
'''

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.folder=Path(self.temp.name)
        (self.folder/'run_agent.py').write_text(AGENT)
        (self.folder/'hermes_cli').mkdir()
        (self.folder/'hermes_cli'/'__init__.py').write_text('')
        (self.folder/'hermes_cli'/'runtime_provider.py').write_text(RUNTIME)
    def tearDown(self): self.temp.cleanup()
    def adapter(self,model='fixture-model'):
        return HermesReasoner(self.folder,model,hermes_python=sys.executable,timeout=10)
    def test_native_runtime_auth_arguments_and_exact_one_call(self):
        result=self.adapter().reason({'events':[]})
        self.assertEqual(result['usage']['nativeCalls'],1)
        self.assertEqual(result['usage']['provider'],'fixture-provider')
        self.assertEqual(result['usage']['inputTokens'],120)
    def test_legacy_reply_in_a_code_fence_is_accepted(self):
        result=self.adapter().reason({'events':[],'reply':'fenced'})
        self.assertEqual(result['decisions'],[])
    def test_legacy_reply_with_prose_around_the_json_is_accepted(self):
        # The single top-level JSON object is extracted (Task 21); cards mode is covered in test_proactive_cards_adapter.
        result=self.adapter().reason({'events':[],'reply':'prose'})
        self.assertEqual(result['decisions'],[])
    def test_stream_entry_counts_one_native_call(self):
        result=self.adapter().reason({'events':[], 'stream_entry':True})
        self.assertEqual(result['usage']['nativeCalls'],1)
    def test_retry_or_grace_and_extra_summary_are_blocked(self):
        for field in ('extra_call','extra_summary'):
            with self.assertRaises(RuntimeError): self.adapter().reason({'events':[],field:True})
    def test_runtime_tools_fail_closed(self):
        with self.assertRaises(RuntimeError): self.adapter('tools-enabled').reason({'events':[]})
    def test_input_ceiling_before_native_process(self):
        with self.assertRaisesRegex(ValueError,'input packet'): self.adapter().reason({'content':'x'*25000})

    def test_system_prompt_counts_toward_input_ceiling(self):
        import json
        from proactive_intelligence.runner import SYSTEM
        packet={'content':'x'*(24000-len(SYSTEM)//2-20)}
        self.assertLessEqual(len(json.dumps(packet)),24000)
        self.assertGreater(len(SYSTEM)+len(json.dumps(packet)),24000)
        with self.assertRaisesRegex(ValueError,'plus system'):self.adapter().reason(packet)

if __name__=='__main__':unittest.main()
