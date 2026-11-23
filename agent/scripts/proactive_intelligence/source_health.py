"""Keep successful vault reads separate from upstream ingestion completeness."""
import time
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

EXPECTED_BRIDGES=('whatsappbot','telegrambot','discordbot','messengerbot','twitterbot')
BRIDGE_STATES={'connected','degraded','failed','down','stale','not deployed','unknown'}

def fresh_bridge_health(run=None, clock=None):
    """Invoke only the existing trusted host checker; return state enums, no detail.

    Never use the six-hour heartbeat as current coverage. This helper is not an
    interactive model tool and grants no new Docker operations or credentials.
    """
    run=run or subprocess.run
    clock=clock or (lambda:int(time.time()*1000))
    repo=Path(__file__).resolve().parents[2]
    try:
        result=run([sys.executable,str(repo/'scripts/matrix_reader.py'),'health'],cwd=str(repo),capture_output=True,text=True,timeout=15,check=False)
        if result.returncode!=0 or not isinstance(result.stdout,str) or len(result.stdout)>32768:return None
        parts=result.stdout.split('--- JSON ---')
        if len(parts)!=2:return None
        raw=json.loads(parts[1]);bridges=raw.get('bridges')
        if not isinstance(bridges,dict):return None
        selected={name:bridges.get(name) for name in EXPECTED_BRIDGES}
        if any(state not in BRIDGE_STATES for state in selected.values()):return None
        if raw.get('synapse') not in ('healthy','unreachable') or raw.get('auth') not in ('valid','failed'):return None
        return {'checkedAt':datetime.fromtimestamp(clock()/1000,timezone.utc).isoformat(),
                'synapse':raw['synapse'],'auth':raw['auth'],'bridges':selected}
    except Exception:return None


def bridge_coverage(receipt,now):
    if not isinstance(receipt,dict):return False
    checked=stamp(receipt.get('checkedAt'))
    if checked is None or not 0<=now-checked<=300000:return False
    bridges=receipt.get('bridges')
    return receipt.get('synapse')=='healthy' and receipt.get('auth')=='valid' and isinstance(bridges,dict) and all(bridges.get(name)=='connected' for name in EXPECTED_BRIDGES)

from .detectors import stamp


class SourceHealthGuard:
    def __init__(self, fetch=None, clock=None, fetch_bridge=None):
        self.fetch=fetch
        self.fetch_bridge=fetch_bridge
        self.clock=clock or (lambda:int(time.time()*1000))
        self.matrix={'status':'unknown','healthy':False,'lastSuccessAt':None,'reason':'source_health_unavailable'}

    def refresh(self):
        self.matrix={'status':'unknown','healthy':False,'lastSuccessAt':None,'reason':'source_health_unavailable'}
        if self.fetch is None:return self.matrix
        try:
            receipt=self.fetch();now=self.clock();checked=stamp(receipt.get('checkedAt'))
            if checked is None or not 0<=now-checked<=300000:raise ValueError('health receipt stale')
            rows=receipt.get('sources')
            if not isinstance(rows,list):raise ValueError('invalid health receipt')
            sources=[r for r in rows if isinstance(r,dict) and r.get('name')=='matrix']
            if len(sources)!=1:raise ValueError('matrix health unknown')
            source=sources[0];status=source.get('status');last=stamp(source.get('lastSuccessAt'));window=source.get('staleAfterMs')
            if status not in ('ok','stale','failing','disabled'):raise ValueError('invalid source status')
            recent=last is not None and isinstance(window,(int,float)) and not isinstance(window,bool) and window>0 and 0<=now-last<=window
            healthy=status=='ok' and recent and source.get('failureStreak',0)==0 and source.get('reconcileOwed',0)==0 and source.get('reconcileStuck',0)==0
            reason='verified_recent_ingestion' if healthy else 'ingestion_freshness_unverified'
            try:covered=self.fetch_bridge is not None and bridge_coverage(self.fetch_bridge(),self.clock())
            except Exception:covered=False
            healthy=healthy and covered
            if not covered:reason='bridge_coverage_unverified'
            elif healthy:reason='verified_ingestion_and_bridges'
            self.matrix={'status':status,'healthy':healthy,'lastSuccessAt':source.get('lastSuccessAt'),'reason':reason}
        except Exception:pass
        return self.matrix

    def filter(self, events):
        eligible=[];blocked=[]
        for event in events:
            if event.get('channel')=='message-thread' and not self.matrix['healthy']:blocked.append(event)
            else:eligible.append(event)
        return {'eligible':eligible,'blocked':blocked}

    def warning(self):
        if self.matrix['healthy']:
            return 'Matrix ingestion and the five configured upstream bridges have recent affirmative health checks. Absence of a vault item alone never proves that no message or reply exists.'
        return 'Matrix ingestion or upstream bridge coverage is unverified ('+self.matrix['reason']+'). Do not use Matrix snapshots as current evidence, and do not infer that no new messages or replies have arrived.'
