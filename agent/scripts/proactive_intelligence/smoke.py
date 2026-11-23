"""Explicit synthetic-only native Hermes smoke. No gateway publication or vault reads.

PYTHONPATH=scripts <Hermes Python> -m proactive_intelligence.smoke \
  --invoke --hermes-path ~/.hermes/hermes-agent --model gpt-5.6-sol \
  --provider openai-codex
"""
import argparse
import json
import tempfile
import time
from pathlib import Path
from .state import State
from .runner import Runner
from .hermes_adapter import HermesReasoner


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--invoke',action='store_true',help='explicitly invoke selected model with synthetic evidence')
    parser.add_argument('--hermes-path',required=True)
    parser.add_argument('--hermes-python')
    parser.add_argument('--model',required=True)
    parser.add_argument('--provider')
    args=parser.parse_args()
    if not args.invoke: parser.error('--invoke required; this smoke spends one model invocation')
    now=int(time.time()*1000)
    with tempfile.TemporaryDirectory(prefix='omni-proactive-smoke-') as folder:
        state=State(Path(folder)/'state.sqlite3')
        state.ingest('synthetic','1',[dict(id='synthetic-message',revision='synthetic-r1',kind='unprocessed',entity='synthetic-message',occurred_at=now,content='Synthetic fixture only. Alex asks whether the fictional garden planning meeting should start at 2pm or 3pm tomorrow. No calendar confirmation exists. Prepare a plain-text draft asking for the intended time; do not claim availability or send it.',url='prism://note/synthetic-message')],now=now,coalesce_ms=0)
        reasoner=HermesReasoner(args.hermes_path,args.model,args.provider,hermes_python=args.hermes_python)
        result=Runner(state,reasoner).tick('This is a synthetic test, not the owner personal data.')
        usage=state.db.execute('SELECT usage FROM runs').fetchone()[0]
        print(json.dumps({'ok':True,'mode':'shadow','decisionCount':len(result['decisions']),'usage':json.loads(usage)}))
        state.db.close()

if __name__=='__main__':main()
