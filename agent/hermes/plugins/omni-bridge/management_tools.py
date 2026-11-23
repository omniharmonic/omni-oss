"""Bounded internal management tool, registered by omni-bridge.

Host-resolved scope is checked on every invocation. The model cannot change
budgets, schedules, privilege, outbound policy or the state location.
"""
import importlib.util
import hashlib
import json
import os
from pathlib import Path

NAME='omni_manage_proactivity'
SCHEMA={
    'name':NAME,
    'description':('List stored preferences, individual-source watcher keys/versions and undo receipts. '
        'For a preference use action=change, kind=preference, top-level key equal to value.key, '
        'and value containing exactly key and value. Example: '
        '{"action":"change","kind":"preference","key":"notification_style",'
        '"value":{"key":"notification_style","value":"concise"}}. '
        'For a watcher use action=change, kind=watcher, a stable top-level watcher key, '
        'and value containing exactly source_id, revision, condition and enabled. '
        'Undo uses action=undo plus receipt_id and expected_version from discovery. '
        'Pending source baselines provide no coverage until recovery. '
        'Inferred changes must be suggested first. Never changes budget or send authority.'),
    'parameters':{
        'type':'object','additionalProperties':False,'required':['action'],
        'properties':{
            'action':{'type':'string','enum':['change','undo','list'],
                'description':'list requires only action. change requires kind, key and value. undo requires receipt_id and expected_version.'},
            'kind':{'type':'string','enum':['preference','watcher'],'description':'Required for change.'},
            'key':{'type':'string','minLength':1,'maxLength':256,
                'description':'Required for change. For preference, the same supported preference name as value.key. For watcher, a stable owner-chosen watcher identifier.'},
            'value':{'type':'object','additionalProperties':False,
                'description':'Required for change. Preference: exactly {key,value}. Watcher: exactly {source_id,revision,condition,enabled}. Never supply source baseline fields.',
                'properties':{
                    'key':{'type':'string','enum':['notification_style','deadline_only','quiet_hours'],
                        'description':'Preference name, required only for kind=preference.'},
                    'value':{'type':['string','boolean'],
                        'description':'Preference value: notification_style uses concise or detailed; deadline_only uses a JSON boolean; quiet_hours uses HH:MM-HH:MM in local time.'},
                    'source_id':{'type':'string','minLength':1,'maxLength':256,
                        'description':'Required for watcher. Existing individual source ID retrieved from trusted source data; project-wide watches are unsupported.'},
                    'revision':{'type':'string','minLength':1,'maxLength':256,
                        'description':'Required for watcher. Exact current source revision; the host verifies it.'},
                    'condition':{'type':'string','enum':['revision_changed','deadline_changed'],
                        'description':'Required for watcher. The change that should trigger attention.'},
                    'enabled':{'type':'boolean','description':'Required for watcher. true enables observation; false disables it.'}
                }},
            'receipt_id':{'type':'string','description':'Required for undo. Use the exact receipt_id returned by discovery or a change receipt.'},
            'expected_version':{'type':'integer','minimum':1,
                'description':'Required for undo. Use that receipt/current item expected_version; a newer change makes an old undo stale.'}
        },
        'examples':[
            {'action':'list'},
            {'action':'change','kind':'preference','key':'notification_style','value':{'key':'notification_style','value':'concise'}},
            {'action':'change','kind':'preference','key':'deadline_only','value':{'key':'deadline_only','value':True}},
            {'action':'change','kind':'preference','key':'quiet_hours','value':{'key':'quiet_hours','value':'22:00-07:00'}},
            {'action':'change','kind':'watcher','key':'deadline-watch','value':{'source_id':'source:1','revision':'r1','condition':'deadline_changed','enabled':True}}
        ]
    }
}

def resolve_watcher_source(root,source_id):
    """Trusted existing read-only adapter; never take baseline fields from args."""
    try:
        import sys
        name='omni_management_parachute_reader'
        module=sys.modules.get(name)
        if module is None:
            spec=importlib.util.spec_from_file_location(name,Path(root)/'scripts/parachute_client.py')
            module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
        note=module.ParachuteClient().get_note(source_id,include_content=False)
        if not note or not note.updated_at:return None
        metadata=note.metadata if isinstance(note.metadata,dict) else {}
        if 'prism-trashed' in (note.tags or []) or metadata.get('prism_trashed_at'):return None
        return {'source_id':note.id,'revision':note.updated_at,'deadlines':[metadata.get(k) for k in ('deadline','due','stated_deadline')]}
    except Exception:return None


def register(ctx,scope,active,repo_root,owner_intent,sync_preferences=None,resolve_source=None):
    def available(): return active() and scope()[0] and scope()[1]=='turn'
    def handler(args,**kwargs):
        if not available(): return json.dumps({'error':'Owner conversation required; scheduled and delegated changes refused'})
        root=Path(repo_root())
        spec=importlib.util.spec_from_file_location('omni_proactive_management',root/'scripts'/'proactive_management.py')
        import sys
        module=sys.modules.get(spec.name)
        if module is None:
            module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        path=Path(os.environ.get('OMNI_PROACTIVE_STATE',str(root/'state'/'proactive.sqlite3')))
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        manager=module.Management(path)
        try:
            if args.get('action')=='list': return json.dumps(manager.discover())
            intent=owner_intent(args, kwargs)
            if not intent: return json.dumps({'error':'Explicit current owner request required'})
            operation=hashlib.sha256(json.dumps(args,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            bound=module.OwnerIntent(intent['turn_id']+':'+operation,intent['owner_id'],True)
            if args.get('action')=='undo': result=manager.undo(args['receipt_id'],args['expected_version'],bound)
            elif args.get('action')=='change':
                snapshot=None
                if args['kind']=='watcher' and args['value'].get('enabled'):
                    reader=resolve_source or (lambda sid:resolve_watcher_source(root,sid))
                    try:snapshot=reader(args['value']['source_id'])
                    except Exception:snapshot=None
                result=manager.change(args['kind'],args['key'],args['value'],bound,source_snapshot=snapshot)
            else:raise ValueError('Unsupported action')
            if result.get('kind') == 'preference':
                snapshot=manager.preference_snapshot()
                result['delivery_preferences_synced']=bool(sync_preferences and sync_preferences(snapshot))
                if not result['delivery_preferences_synced']:
                    result['timing']='Stored; attention delivery update pending coordinator retry'
            return json.dumps(result)
        except (ValueError,PermissionError,KeyError) as exc:return json.dumps({'error':str(exc)})
        finally:manager.db.close()
    ctx.register_tool(name=NAME,toolset='omni',schema=SCHEMA,handler=handler,check_fn=available,emoji='👁')
