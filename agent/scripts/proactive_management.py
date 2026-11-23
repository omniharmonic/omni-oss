"""Owner-requested internal preferences. No scheduler, model or outbound authority.

The gateway must bind OwnerIntent to its authenticated current human turn. Never
construct it from a model tool argument or from retrieved correspondence.
"""
from dataclasses import dataclass
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: The owner's IANA timezone (identity owner.timezone)

@dataclass(frozen=True)
class OwnerIntent:
    turn_id: str
    owner_id: str
    explicit: bool

PREFERENCES = {
    'notification_style': {'concise', 'detailed'},
    'deadline_only': {True, False},
    'quiet_hours': None,
}

def validate(kind, value):
    if not isinstance(value, dict):
        raise ValueError("Management value must be an object")
    if kind == 'preference':
        if set(value) != {'key', 'value'} or value['key'] not in PREFERENCES:
            raise ValueError('Unsupported preference; budget and authority are immutable here')
        allowed = PREFERENCES[value['key']]
        if value['key'] == 'deadline_only' and type(value['value']) is not bool:
            raise ValueError('deadline_only requires a boolean')
        if allowed is not None and (not isinstance(value['value'], (str, bool)) or value['value'] not in allowed):
            raise ValueError('Invalid preference value')
        if value['key'] == 'quiet_hours':
            import re
            if not isinstance(value['value'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d', value['value']):
                raise ValueError('Quiet hours require HH:MM-HH:MM in ' + OWNER_TZ)
    elif kind == 'watcher':
        if set(value) != {'source_id', 'revision', 'condition', 'enabled'}:
            raise ValueError('Watcher requires source_id, revision, condition, enabled')
        if value['condition'] not in {'revision_changed', 'deadline_changed'} or type(value['enabled']) is not bool:
            raise ValueError('Unsupported watcher condition')
        if any(not isinstance(value[k], str) or not value[k] or len(value[k]) > 256 for k in ('source_id', 'revision')):
            raise ValueError('Bounded source identity and revision required')
    else:
        raise ValueError('Unsupported management kind')

class Management:
    def __init__(self, path):
        if str(path) != ':memory:':
            descriptor=os.open(str(path),os.O_CREAT|os.O_RDWR,0o600);os.close(descriptor)
            os.chmod(str(path),0o600)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS management_values(kind TEXT,key TEXT,version INTEGER,value TEXT,PRIMARY KEY(kind,key));
        CREATE TABLE IF NOT EXISTS management_watcher_baselines(key TEXT,version INTEGER,source_id TEXT,revision TEXT,baseline TEXT,status TEXT,captured_at TEXT,PRIMARY KEY(key,version));
        CREATE TABLE IF NOT EXISTS detector_deadlines(id TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS management_receipts(id TEXT PRIMARY KEY,turn_id TEXT UNIQUE,kind TEXT,key TEXT,version INTEGER,before_value TEXT,after_value TEXT,created_at TEXT,undo_of TEXT);
        ''')

    def change(self, kind, key, value, intent, source_snapshot=None):
        if not isinstance(intent, OwnerIntent) or not intent.explicit or not intent.owner_id or not intent.turn_id:
            raise PermissionError('An authenticated explicit owner request is required; inferred changes are suggestions')
        validate(kind, value)
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError('Invalid management key')
        if kind == 'preference' and key != value['key']:
            raise ValueError('Preference key mismatch')
        encoded = json.dumps(value, sort_keys=True)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            prior = self.db.execute('SELECT id,kind,key,after_value FROM management_receipts WHERE turn_id=?', (intent.turn_id,)).fetchone()
            if prior:
                if prior[1:] != (kind, key, encoded):
                    raise ValueError('Owner turn already consumed by a different change')
                return self.receipt(prior[0])
            if kind == 'watcher' and source_snapshot is not None:
                if source_snapshot.get('source_id') != value['source_id'] or source_snapshot.get('revision') != value['revision']:
                    raise ValueError('Source changed before registration; re-read its revision and retry')
            row = self.db.execute('SELECT version,value FROM management_values WHERE kind=? AND key=?', (kind,key)).fetchone()
            version = row[0]+1 if row else 1
            receipt = uuid.uuid4().hex
            self.db.execute('INSERT OR REPLACE INTO management_values VALUES(?,?,?,?)',(kind,key,version,encoded))
            if kind == 'watcher':
                self._record_baseline(key,version,value,source_snapshot)
            self.db.execute('INSERT INTO management_receipts VALUES(?,?,?,?,?,?,?,?,NULL)',(receipt,intent.turn_id,kind,key,version,row[1] if row else None,encoded,datetime.now(timezone.utc).isoformat()))
        return self.receipt(receipt)

    def receipt(self, receipt):
        row = self.db.execute('SELECT id,kind,key,version,after_value,undo_of FROM management_receipts WHERE id=?',(receipt,)).fetchone()
        if not row:
            raise ValueError('Unknown receipt')
        result = dict(zip(('id','kind','key','version','value','undo_of'),(row[0],row[1],row[2],row[3],json.loads(row[4]) if row[4] else None,row[5])), timezone=OWNER_TZ, budget='unchanged', authority='unchanged', timing='next reconciliation after activation', undo={'receipt_id':row[0],'expected_version':row[3]})
        result['created_at']=self.db.execute('SELECT created_at FROM management_receipts WHERE id=?',(receipt,)).fetchone()[0]
        current=self.db.execute('SELECT version FROM management_values WHERE kind=? AND key=?',row[1:3]).fetchone()
        result['undo_available']=bool(current and current[0]==row[3])
        if row[1] == 'watcher':
            result['baseline']=self._baseline_info(row[2],row[3]) if row[4] is not None else {'status':'removed','coverage_from':None}
            if result['baseline']['status'] == 'pending':
                result['timing']='Stored; source baseline pending. Coverage begins at the first successful trusted read; earlier changes cannot be inferred.'
        return result

    def undo(self, receipt_id, expected_version, intent):
        if not isinstance(intent, OwnerIntent) or not intent.explicit or not intent.turn_id or not intent.owner_id:
            raise PermissionError('Authenticated owner request required')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            replay = self.db.execute('SELECT id,undo_of FROM management_receipts WHERE turn_id=?',(intent.turn_id,)).fetchone()
            if replay:
                if replay[1] != receipt_id:
                    raise ValueError('Owner turn already consumed by a different change')
                return self.receipt(replay[0])
            old = self.db.execute('SELECT kind,key,version,before_value FROM management_receipts WHERE id=?',(receipt_id,)).fetchone()
            if not old:
                raise ValueError('Unknown receipt')
            current = self.db.execute('SELECT version,value FROM management_values WHERE kind=? AND key=?',old[:2]).fetchone()
            if not current or current[0] != expected_version or old[2] != expected_version:
                raise ValueError('Changed since receipt; review current preference before undo')
            version = current[0]+1
            rid = uuid.uuid4().hex
            self.db.execute('UPDATE management_values SET version=?,value=? WHERE kind=? AND key=?',(version,old[3],*old[:2]))
            if old[0] == 'watcher' and old[3] is not None:
                prior=self.db.execute('SELECT revision,baseline,status,captured_at FROM management_watcher_baselines WHERE key=? AND version=?',(old[1],old[2]-1)).fetchone()
                restored=json.loads(old[3])
                if prior:
                    self.db.execute('INSERT OR REPLACE INTO management_watcher_baselines VALUES(?,?,?,?,?,?,?)',(old[1],version,restored['source_id'],*prior))
                else:self._record_baseline(old[1],version,restored,None)
            self.db.execute('INSERT INTO management_receipts VALUES(?,?,?,?,?,?,?,?,?)',(rid,intent.turn_id,*old[:2],version,current[1],old[3],datetime.now(timezone.utc).isoformat(),receipt_id))
        return self.receipt(rid)

    def preference_snapshot(self):
        rows=self.db.execute("SELECT key,version,value FROM management_values WHERE kind='preference'").fetchall()
        return {'version':sum(row[1] for row in rows), 'preferences':{row[0]:json.loads(row[2])['value'] for row in rows if row[2] is not None}}

    def preferences(self):
        return {row[0]:json.loads(row[1])['value'] for row in self.db.execute("SELECT key,value FROM management_values WHERE kind='preference' AND value IS NOT NULL")}

    def _record_baseline(self,key,version,value,snapshot):
        ready=snapshot is not None
        status='disabled' if not value['enabled'] else 'ready' if ready else 'pending'
        baseline=json.dumps(snapshot['deadlines'],sort_keys=True) if ready else None
        revision=snapshot['revision'] if ready else value['revision']
        captured=datetime.now(timezone.utc).isoformat() if ready else None
        self.db.execute('INSERT OR REPLACE INTO management_watcher_baselines VALUES(?,?,?,?,?,?,?)',(key,version,value['source_id'],revision,baseline,status,captured))
        if ready and value['condition']=='deadline_changed' and value['enabled']:
            # Do not reset another active source watch's unacknowledged baseline.
            others=self.db.execute("SELECT key,version,value FROM management_values WHERE kind='watcher' AND key<>? AND value IS NOT NULL",(key,)).fetchall()
            shared=any(json.loads(row[2]).get('enabled') and json.loads(row[2]).get('condition')=='deadline_changed' and json.loads(row[2]).get('source_id')==value['source_id'] and self._baseline_info(row[0],row[1])['status']=='ready' for row in others)
            if not shared:
                self.db.execute('INSERT OR REPLACE INTO detector_deadlines VALUES(?,?)',(value['source_id'],baseline))

    def _baseline_info(self,key,version):
        row=self.db.execute('SELECT revision,status,captured_at FROM management_watcher_baselines WHERE key=? AND version=?',(key,version)).fetchone()
        return {'revision':row[0], 'status':row[1], 'coverage_from':row[2]} if row else {'status':'pending','coverage_from':None}

    def refresh_pending_watchers(self,notes):
        """Trusted collector index only; unavailable periods are not reconstructed."""
        by_id={note.id:note for note in notes}
        activated=[]
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for key,version,encoded in self.db.execute("SELECT key,version,value FROM management_values WHERE kind='watcher' AND value IS NOT NULL").fetchall():
                value=json.loads(encoded)
                if not value['enabled'] or self._baseline_info(key,version)['status']!='pending':continue
                note=by_id.get(value['source_id'])
                if not note or not note.updated_at:continue
                metadata=note.metadata if isinstance(note.metadata,dict) else {}
                if 'prism-trashed' in (note.tags or []) or metadata.get('prism_trashed_at'):continue
                self._record_baseline(key,version,value,{'source_id':note.id,'revision':note.updated_at,'deadlines':[metadata.get(k) for k in ('deadline','due','stated_deadline')]})
                activated.append({'key':key,'version':version,'coverage_from':self._baseline_info(key,version)['coverage_from']})
        return activated

    def discover(self,receipt_limit=20):
        if type(receipt_limit) is not int or not 1<=receipt_limit<=100:raise ValueError('Receipt limit must be 1..100')
        items=[]
        for kind,key,version,encoded in self.db.execute('SELECT kind,key,version,value FROM management_values ORDER BY kind,key'):
            latest=self.db.execute('SELECT id FROM management_receipts WHERE kind=? AND key=? AND version=?',(kind,key,version)).fetchone()
            item={'kind':kind,'key':key,'version':version,'value':json.loads(encoded) if encoded else None,'undo':{'receipt_id':latest[0],'expected_version':version} if latest else None}
            if kind=='watcher':item['baseline']=self._baseline_info(key,version) if encoded is not None else {'status':'removed','coverage_from':None}
            items.append(item)
        receipts=[self.receipt(row[0]) for row in self.db.execute('SELECT id FROM management_receipts ORDER BY rowid DESC LIMIT ?',(receipt_limit,)).fetchall()]
        help={
            'supported_preferences':{
                'notification_style':{'value_type':'string','allowed_values':['concise','detailed']},
                'deadline_only':{'value_type':'boolean','allowed_values':[False,True]},
                'quiet_hours':{'value_type':'string','format':'HH:MM-HH:MM','timezone':OWNER_TZ}},
            'supported_watch_conditions':['revision_changed','deadline_changed'],
            'change_required':['action','kind','key','value'],
            'preference_value_required':['key','value'],
            'watcher_value_required':['source_id','revision','condition','enabled'],
            'preference_example':{'action':'change','kind':'preference','key':'notification_style','value':{'key':'notification_style','value':'concise'}},
            'undo_instruction':'Use action=undo plus the current item undo.receipt_id and undo.expected_version. Top-level preference key must equal value.key.'}
        return {'help':help,'scope':'stored management state; delivery mirror is verified separately','preferences':self.preference_snapshot(),'items':items,'receipts':receipts,'receipt_limit':receipt_limit,'watch_scope':'individual sources only'}

    def active_watchers(self):
        result=[]
        for key,version,encoded in self.db.execute("SELECT key,version,value FROM management_values WHERE kind='watcher' AND value IS NOT NULL"):
            value=json.loads(encoded)
            if not value['enabled']:continue
            row=self.db.execute('SELECT revision,baseline,status FROM management_watcher_baselines WHERE key=? AND version=?',(key,version)).fetchone()
            if not row or row[2]!='ready':continue
            if value['condition']=='revision_changed':value['revision']=row[0]
            if row and row[1] is not None:value.update(baseline_revision=row[0],deadline_baseline=row[1])
            result.append(value)
        return result
