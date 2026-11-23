#!/usr/bin/env python3
"""Named installer for the agent host: dry run default; --apply or --rollback receipt directory.
Uses native Hermes public jobs API with cross-process locking; never writes cron/jobs.json.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import inspect
import sys
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: the owner's IANA timezone (identity owner.timezone)

NAME='omni-proactive-hourly'
SCHEDULE='5 8-20 * * *'
SCRIPT='omni-proactive-hourly.py'
HEALTH_NAME='omni-health'
HEALTH_SCHEDULE='0 21 * * *'
HEALTH_TIMEZONE=OWNER_TZ  # create_job takes no timezone: Hermes' configured zone applies; recorded in the plan only
MEMORY_SYNC_NAME='omni-hermes-memory-sync'
MEMORY_SYNC_SCHEDULE='17 * * * *'  # hourly: Hermes memory -> Parachute mirror, a no-op unless MEMORY.md / USER.md changed
ROUTINE_SCHEDULES={'events':'* 8-20 * * *','morning':'15 7 * * *','evening':'0 19 * * *','tweet':'0 10 * * *','nightly-review':'30 5 * * *'}

def routine_recipes(repo,home):
    model,provider=configured_role(home)
    return [{'name':'omni-proactive-'+name,'schedule':schedule,'timezone':OWNER_TZ,
             'enabled':False,'no_agent':True,'deliver':'local',
             'script':'omni-proactive-'+name+'.py','model':model,'provider':provider,
             'arguments':(['--event-intake','--reconcile'] if name=='events' else ['--routine',name])+['--state',str(repo/'state/proactive.sqlite3'),'--reason','--live'],
             'activation':'individual parity acceptance required; preserve cloud routines'}
            for name,schedule in ROUTINE_SCHEDULES.items()]


def configured_role(home):
    """Operator configuration only; never a model tool argument or fallback."""
    import yaml
    config=yaml.safe_load((home/'config.yaml').read_text()) if (home/'config.yaml').exists() else {}
    role=(config.get('roles') or {}).get('proactive') or config.get('model') or {}
    if not isinstance(role,dict):role={'default':role}
    from dotenv import dotenv_values
    environment=dotenv_values(home/'.env')
    return (os.environ.get('OMNI_PROACTIVE_MODEL') or environment.get('OMNI_PROACTIVE_MODEL') or role.get('model') or role.get('default') or 'gpt-5.6-sol',
            os.environ.get('OMNI_PROACTIVE_PROVIDER') or environment.get('OMNI_PROACTIVE_PROVIDER') or role.get('provider') or 'openai-codex')

def _wrapper_python(repo):
    """The interpreter a wrapper starts in: the repo's own venv (python-dotenv), never Hermes'.
    Hermes' runtime moves on every update (PM environments since v0.23); the wrapper asks
    Hermes' launcher for it at run time (scripts/hermes_runtime.py)."""
    return str(repo/'venv/bin/python')

def wrapper(repo,home,model,provider,routine=None):
    args=['--state',str(repo/'state/proactive.sqlite3'),'--reconcile','--reason','--live','--hermes-path',str(home/'hermes-agent'),'--model',model,'--provider',provider]
    if routine=='events':args.append('--event-intake')
    elif routine:
        args.remove('--reconcile');args.extend(['--routine',routine])
    return '#!'+_wrapper_python(repo)+'\n'+'''import os, sys
from pathlib import Path
from dotenv import dotenv_values
''' + f'''repo=Path({str(repo)!r})
home=Path({str(home)!r})
for file in (repo/'.env',home/'.env'):
    for key,value in dotenv_values(file).items():
        if value is not None: os.environ.setdefault(key,value)
os.chdir(repo/'scripts')
sys.path.insert(0,str(repo/'scripts'))
from hermes_runtime import module_argv
exe,argv=module_argv(home,'proactive_intelligence',{args!r},extra_paths=[str(repo/'scripts')])
os.execv(exe,argv)
'''

class NativeJobs:
    """Use installed Hermes' public mutators only with its cross-process lock."""
    def __init__(self,home):
        os.environ['HERMES_HOME']=str(home)
        sys.path.insert(0,str(home/'hermes-agent'))
        from cron import jobs
        lock=getattr(jobs,'_jobs_lock',None)
        if lock is None or 'flock' not in inspect.getsource(lock):
            raise ValueError('Installed Hermes lacks verified cross-process scheduler locking')
        self.jobs=jobs
    def call(self,method,path,body=None):
        jobs=self.jobs
        if method=='GET':return {'jobs':jobs.list_jobs(include_disabled=True)}
        jid=path.split('/')[3] if len(path.split('/'))>3 else None
        if path.endswith('/pause'):result=jobs.pause_job(jid)
        elif path.endswith('/resume'):result=jobs.resume_job(jid)
        elif method=='PATCH':result=jobs.update_job(jid,dict(body))
        elif method=='POST':
            with jobs._jobs_lock():
                if any(j.get('name')==body['name'] for j in jobs.list_jobs(include_disabled=True)):
                    raise ValueError('Job name already exists; reconcile before retry')
                result=jobs.create_job(**{k:v for k,v in body.items() if k!='enabled'})
                if body.get('enabled') is False:
                    result=jobs.pause_job(result['id'])
        else:raise ValueError('Unsupported scheduler operation')
        if not result:raise ValueError('Scheduler did not confirm mutation')
        return {'job':result}

def health_wrapper(repo,home):
    python=_wrapper_python(repo)
    return '#!'+python+'\n'+f'''import os
from pathlib import Path
from dotenv import dotenv_values
repo=Path({str(repo)!r})
home=Path({str(home)!r})
for file in (repo/'.env',home/'.env'):
    for key,value in dotenv_values(file).items():
        if value is not None: os.environ.setdefault(key,value)
os.chdir(repo/'scripts')
os.execv({python!r},[{python!r},{str(repo/'scripts/omni_health.py')!r},'--state',{str(repo/'state/proactive.sqlite3')!r}])
'''

#: The only variables the memory mirror needs (ParachuteClient). Taken from the repo .env, which wins over
#: anything inherited from the cron environment; nothing else from either .env reaches the process environment.
MEMORY_SYNC_ENV=('PARACHUTE_URL','PARACHUTE_VAULT','PARACHUTE_API_KEY')

def memory_sync_wrapper(repo,home):
    python=_wrapper_python(repo)   # stdlib + ParachuteClient only; Hermes' memory is read as files
    return '#!'+python+'\n'+f'''import os
from pathlib import Path
from dotenv import dotenv_values
repo=Path({str(repo)!r})
home=Path({str(home)!r})
values=dotenv_values(repo/'.env')
env={{k:os.environ[k] for k in ('PATH','HOME','LANG','TMPDIR') if k in os.environ}}
env.update({{k:values[k] for k in {MEMORY_SYNC_ENV!r} if values.get(k) is not None}})
env['HERMES_HOME']=str(home)
os.chdir(repo/'scripts')
os.execve({python!r},[{python!r},{str(repo/'scripts/hermes_memory_sync.py')!r},'--hermes-home',{str(home)!r},'--state',{str(repo/'state/hermes-memory-sync.json')!r}],env)
'''

def install(api,repo,home,receipt,apply=False,model=None,provider=None,routine=None,enable=False,paused=False,health=False,memory_sync=False):
    os.umask(0o077)
    if health and memory_sync:raise ValueError('--health and --memory-sync are separate installs')
    if health or memory_sync:
        if routine or model or provider:raise ValueError(('--health' if health else '--memory-sync')+' cannot be combined with --routine, --model or --provider')
    if paused and enable:raise ValueError('--paused and --enable cannot be combined')
    if routine is not None and routine not in ROUTINE_SCHEDULES:raise ValueError('Unknown routine')
    name=HEALTH_NAME if health else MEMORY_SYNC_NAME if memory_sync else 'omni-proactive-'+routine if routine else NAME
    schedule=HEALTH_SCHEDULE if health else MEMORY_SYNC_SCHEDULE if memory_sync else ROUTINE_SCHEDULES[routine] if routine else SCHEDULE
    script_name=name+'.py'
    enabled=False if paused else (enable if routine else True)
    if health or memory_sync:
        body={'name':name,'schedule':schedule,'prompt':'','script':script_name,'no_agent':True,'deliver':'local','enabled':enabled}
        source=health_wrapper(repo,home) if health else memory_sync_wrapper(repo,home)
    else:
        configured_model,configured_provider=configured_role(home)
        model=model or configured_model;provider=provider or configured_provider
        if (model,provider)!=(configured_model,configured_provider):raise ValueError('Requested model differs from operator proactive role configuration')
        body={'name':name,'schedule':schedule,'prompt':'','script':script_name,'no_agent':True,'deliver':'local','model':model,'provider':provider,'enabled':enabled}
        source=wrapper(repo,home,model,provider,routine)
    jobs=api.call('GET','/api/jobs?include_disabled=true')['jobs']
    matches=[j for j in jobs if j.get('name')==name]
    if len(matches)>1:raise ValueError('Duplicate proactive name; resolve before installation')
    plan={'job':body,'operation':'update' if matches else 'create','script_sha256':hashlib.sha256(source.encode()).hexdigest(),'legacy_changes':False}
    if health:plan['timezone']=HEALTH_TIMEZONE
    if not apply:return plan
    receipt=Path(receipt)
    if receipt.exists():raise ValueError('Fresh named receipt directory required')
    receipt.mkdir(parents=True,mode=0o700)
    script=home/'scripts'/script_name;script.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    record={'job_name':name,'previous_job':json.loads(json.dumps(matches[0])) if matches else None,'script_existed':script.exists(),'script':str(script)}
    if script.exists():(receipt/'script-before.py').write_bytes(script.read_bytes())
    (receipt/'jobs-before.json').write_text(json.dumps({'jobs':jobs},indent=2)+'\n')
    (receipt/'receipt.json').write_text(json.dumps(record,indent=2)+'\n')
    script.write_text(source);script.chmod(0o700)
    # API uncertainty is recorded before dispatch. Re-run list; never blindly create twice.
    record['dispatch']='unknown';(receipt/'receipt.json').write_text(json.dumps(record,indent=2)+'\n')
    if matches:result=api.call('PATCH','/api/jobs/'+matches[0]['id'],body)['job']
    else:result=api.call('POST','/api/jobs',body)['job']
    record.update(job_id=result['id'],dispatch='settings-updated',installed=body)
    (receipt/'receipt.json').write_text(json.dumps(record,indent=2)+'\n')
    # Updating enabled alone may leave Hermes' durable state paused. Use its
    # lifecycle operation, then verify persisted state rather than update output.
    action='resume' if enabled else 'pause'
    api.call('POST','/api/jobs/'+result['id']+'/'+action,{})
    fresh=api.call('GET','/api/jobs?include_disabled=true')['jobs']
    verified=[j for j in fresh if j.get('name')==name and j.get('id')==result['id']]
    if len(verified)!=1:
        raise ValueError('Scheduler did not confirm the installed job identity')
    result=verified[0]
    if enabled and result.get('state') in ('paused','disabled') or not enabled and result.get('state') not in (None,'paused','disabled'):
        api.call('POST','/api/jobs/'+result['id']+'/pause',{})
        raise ValueError('Scheduler lifecycle state mismatch; job paused')
    for k in ('script','no_agent','deliver','enabled'):
        if result.get(k)!=body[k]:
            api.call('POST','/api/jobs/'+result['id']+'/pause',{})
            raise ValueError('Hermes rejected required script-only contract; job paused; use receipt rollback')
    record['dispatch']='confirmed';record['verified_state']=result.get('state')
    (receipt/'receipt.json').write_text(json.dumps(record,indent=2)+'\n')
    return {**plan,'job_id':result['id'],'receipt':str(receipt)}

def rollback(api,receipt,apply=False):
    receipt=Path(receipt);r=json.loads((receipt/'receipt.json').read_text())
    jobs=api.call('GET','/api/jobs?include_disabled=true')['jobs']
    name=r.get('job_name',NAME)
    matched=[j for j in jobs if j.get('name')==name]
    if len(matched)>1:raise ValueError('Ambiguous job outcome; review inventory')
    jid=r.get('job_id') or (matched[0]['id'] if matched else None)
    if not apply:return {'rollback_job_id':jid,'restore_previous':r['previous_job'] is not None,'legacy_changes':False}
    if jid:
        api.call('POST','/api/jobs/'+jid+'/pause',{})
        previous=r['previous_job']
        if previous:
            fields={k:previous.get(k) for k in ('name','schedule','prompt','script','no_agent','deliver','model','provider','enabled')}
            if isinstance(fields['schedule'],dict):fields['schedule']=fields['schedule'].get('expr') or fields['schedule'].get('cron')
            api.call('PATCH','/api/jobs/'+jid,fields)

    script=Path(r['script'])
    if r['script_existed']:script.write_bytes((receipt/'script-before.py').read_bytes())
    elif script.exists():script.unlink()
    if jid:
        desired=bool(r['previous_job'] and r['previous_job'].get('enabled',True))
        api.call('POST','/api/jobs/'+jid+('/resume' if desired else '/pause'),{})
        fresh=api.call('GET','/api/jobs?include_disabled=true')['jobs']
        verified=[j for j in fresh if j.get('id')==jid and j.get('name')==name]
        if len(verified)!=1 or verified[0].get('enabled') is not desired or (desired and verified[0].get('state') in ('paused','disabled')) or (not desired and verified[0].get('state') not in (None,'paused','disabled')):
            api.call('POST','/api/jobs/'+jid+'/pause',{})
            raise ValueError('Rollback lifecycle verification failed; job paused')
    return {'rolled_back':True,'new_job_retained_paused':r['previous_job'] is None}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo',type=Path,default=Path(__file__).resolve().parent.parent)
    p.add_argument('--hermes-home',type=Path,default=Path.home()/'.hermes')
    p.add_argument('--receipt',type=Path)
    p.add_argument('--routine',choices=list(ROUTINE_SCHEDULES))
    p.add_argument('--paused',action='store_true',help='stage hourly or routine job paused without activation')
    p.add_argument('--enable',action='store_true',help='enable an individually verified routine; default routine install is paused')
    p.add_argument('--health',action='store_true',help='install the daily omni-health output check (21:00 local, enabled)')
    p.add_argument('--memory-sync',action='store_true',help='install the hourly Hermes memory -> Parachute mirror (scripts/hermes_memory_sync.py, enabled)')
    p.add_argument('--routine-recipes',action='store_true',help='print paused morning/evening/tweet/nightly-review staging recipes only')
    p.add_argument('--apply',action='store_true');p.add_argument('--rollback',action='store_true')
    p.add_argument('--model');p.add_argument('--provider')
    a=p.parse_args()
    if a.paused and a.enable:p.error("--paused and --enable cannot be combined")
    if a.routine_recipes:
        print(json.dumps(routine_recipes(a.repo,a.hermes_home),indent=2));raise SystemExit(0)
    if not a.receipt:p.error('--receipt is required for installation or rollback')
    api=NativeJobs(a.hermes_home)
    try:result=rollback(api,a.receipt,a.apply) if a.rollback else install(api,a.repo,a.hermes_home,a.receipt,a.apply,a.model,a.provider,a.routine,a.enable,a.paused,a.health,a.memory_sync)
    except Exception as exc:raise SystemExit('Installation unavailable: '+type(exc).__name__+'; inspect named receipt and re-list before retry')
    print(json.dumps(result,indent=2))
