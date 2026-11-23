"""Metadata-only event admission and process lock shared with hourly/synthesis."""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.request import Request
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: the owner's IANA timezone (identity owner.timezone)

#: A coordinator run is a bounded model call (HermesReasoner.timeout 180 s, cards 105 s) plus Hermes'
#: background review (150 s) and a few 15 s HTTP calls, so a legitimate holder is done in well under
#: 10 minutes. Past this bound the holder is wedged, not busy. On 2026-11-08 an --event-intake run was
#: SIGSTOPped when the Hermes gateway was killed and held this lock for three days: every later run
#: skipped silently and the daily tweet routine failed "coordinator busy". A slower box can raise it
#: with OMNI_COORDINATOR_MAX_HOLD_S.
MAX_HOLD_S = int(os.environ.get('OMNI_COORDINATOR_MAX_HOLD_S') or 900)
#: Consecutive coordinator-busy skips (non-routine path) before the run exits non-zero, so Hermes
#: Jobs / omni-health show the job red instead of a silent `ok`.
BUSY_FAIL_AFTER = 10

def _log(message):
    print('coordinator-lock: '+message, file=sys.stderr, flush=True)

def _lock_path(path):return str(path)+'.coordinator.lock'

def _write_holder(fd):
    """Record who holds the lock, so a later run can tell a wedged holder from a busy one."""
    data=json.dumps({'pid':os.getpid(),'startedAt':int(time.time())}).encode()
    os.ftruncate(fd,0);os.pwrite(fd,data,0)

def _clear_holder(fd):
    try:os.ftruncate(fd,0)
    except OSError:pass

def _read_holder(fd):
    try:
        value=json.loads(os.pread(fd,4096,0).decode() or 'null')
        pid,started=value.get('pid'),value.get('startedAt')
        if isinstance(pid,int) and pid>1 and isinstance(started,int):return pid,started
    except (OSError,ValueError,AttributeError):pass
    return None

def _etime_seconds(text):
    """ps etime: [[dd-]hh:]mm:ss -> seconds."""
    days,_,rest=text.rpartition('-')
    parts=[int(x) for x in rest.split(':')]
    while len(parts)<3:parts.insert(0,0)
    return (int(days) if days else 0)*86400+parts[0]*3600+parts[1]*60+parts[2]

def process_info(pid):
    """(stat, elapsed seconds) of a live process; None if it is gone or a zombie; 'unknown' if ps failed."""
    try:
        out=subprocess.run(['ps','-o','stat=,etime=','-p',str(pid)],capture_output=True,text=True,timeout=5)
    except (OSError,subprocess.SubprocessError):return 'unknown'
    fields=out.stdout.split()
    if out.returncode!=0 or len(fields)<2 or fields[0].startswith('Z'):return None
    try:return fields[0],_etime_seconds(fields[1])
    except ValueError:return fields[0],None

def _wait_gone(pid,seconds,info,sleep):
    for _ in range(int(seconds/0.25)):
        if info(pid) is None:return True
        sleep(0.25)
    return info(pid) is None

def _try_flock(fd,attempts=1,sleep=time.sleep):
    for i in range(attempts):
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);return True
        except BlockingIOError:
            if i+1<attempts:sleep(0.25)
    return False

def reclaim_stale(fd,max_hold_s=None,info=process_info,kill=os.kill,sleep=time.sleep,now=time.time):
    """The lock is busy. Take it over only from a holder that is provably not making progress: stopped
    (state T), or holding past max_hold_s. A live, running, in-bound holder is never touched, nor one
    without a holder record. Returns True when this process now holds the lock."""
    max_hold_s=MAX_HOLD_S if max_hold_s is None else max_hold_s
    holder=_read_holder(fd)
    if holder is None:return False  # no record (predates this code, or mid-write): never guess
    pid,started=holder
    if pid==os.getpid():return False
    held=max(0,int(now())-started)
    state=info(pid)
    if state=='unknown':return False
    if state is None:
        # Holder is gone yet the lock is busy: a new holder racing to write its record. Retry briefly only.
        return _try_flock(fd,8,sleep)
    stat,elapsed=state
    if elapsed is not None and elapsed+60<held:
        return False  # pid reused by an unrelated, younger process: not the holder
    stopped=stat.startswith('T')
    if not stopped and held<=max_hold_s:return False
    why='stopped (state %s)'%stat if stopped else 'holding it for %ds, over the %ds bound'%(held,max_hold_s)
    _log('holder pid %d is %s; terminating it and taking the lock'%(pid,why))
    for sig in (signal.SIGCONT,signal.SIGTERM):
        try:kill(pid,sig)
        except ProcessLookupError:break
        except PermissionError as error:
            _log('cannot signal pid %d: %s'%(pid,error));return False
    if not _wait_gone(pid,10,info,sleep):
        _log('pid %d ignored SIGTERM; sending SIGKILL'%pid)
        try:kill(pid,signal.SIGKILL)
        except ProcessLookupError:pass
        _wait_gone(pid,5,info,sleep)
    if _try_flock(fd,20,sleep):
        _log('lock reclaimed from pid %d'%pid);return True
    _log('pid %d terminated but the lock is still busy'%pid);return False

@contextmanager
def coordinator_lock(path,max_hold_s=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd=os.open(_lock_path(path),os.O_CREAT|os.O_RDWR,0o600)
    try:
        if not _try_flock(fd) and not reclaim_stale(fd,max_hold_s):
            yield False
            return
        _write_holder(fd)
        try:yield True
        finally:_clear_holder(fd)
    finally:os.close(fd)

@contextmanager
def coordinator_lock_wait(path, wait_s, poll_s=2.0, sleep=time.sleep, clock=time.monotonic, max_hold_s=None):
    """coordinator_lock for a daily routine: wait up to wait_s (polling every poll_s) instead of
    skipping, because the every-minute events job holds it often. Yields False only after the wait."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd=os.open(_lock_path(path),os.O_CREAT|os.O_RDWR,0o600)
    try:
        deadline=clock()+wait_s
        while True:
            if _try_flock(fd) or reclaim_stale(fd,max_hold_s):break
            if clock()>=deadline:
                yield False
                return
            sleep(poll_s)
        _write_holder(fd)
        try:yield True
        finally:_clear_holder(fd)
    finally:os.close(fd)

def record_busy(path,busy):
    """Count consecutive coordinator-busy skips in a side file (not the sqlite state, which the holder
    may be using). Returns the current streak; a successful acquisition resets it to 0."""
    file=Path(str(path)+'.coordinator.busy')
    if not busy:
        try:file.unlink()
        except FileNotFoundError:pass
        return 0
    try:count=int(json.loads(file.read_text()).get('count',0))
    except (OSError,ValueError,AttributeError):count=0
    count+=1
    try:file.write_text(json.dumps({'count':count,'at':int(time.time())}))
    except OSError:pass
    return count

class Intake:
    def __init__(self,url,token,fetch=None):
        from .runner import GatewayPublisher
        gateway=GatewayPublisher(url,token)
        self.url=gateway.url+'/intake'
        self.opener=gateway.opener
        self.token=token
        self.fetch=fetch or self._fetch
    def _fetch(self,path,body):
        if not self.token:raise ValueError('event intake requires service token')
        request=Request(self.url+path,data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with self.opener.open(request,timeout=15) as response:return json.load(response)
    def ready(self,now):
        local=datetime.fromtimestamp(now/1000,ZoneInfo(OWNER_TZ))
        if not 8<=local.hour<=20:return None
        value=self.fetch('',{})
        generation=value.get('generation');ready=value.get('readyAt')
        if not isinstance(generation,int) or isinstance(generation,bool) or generation<0:raise ValueError('invalid intake generation')
        if value.get('pending') is not True:return None
        if not isinstance(ready,int) or isinstance(ready,bool) or ready<0:raise ValueError('invalid intake readiness')
        return generation if ready<=now else None
    def acknowledge(self,generation):
        return self.fetch('/ack',{'generation':generation})

def safe_to_ack(state):
    # Queued budget-limited work is durable and hourly will recover it. Unknown
    # model/publication outcomes retain the signal for operator reconciliation.
    return not state.db.execute("SELECT 1 FROM runs WHERE status IN ('running','unknown')").fetchone() and not state.db.execute("SELECT 1 FROM outbox WHERE status='unknown'").fetchone() and not state.db.execute("SELECT 1 FROM sources WHERE health<>'healthy' AND name='vault'").fetchone()

class Backoff:
    """Persist an admission hold before execution; failure/restart keeps it."""
    def __init__(self,state):
        self.db=state.db
        self.db.execute('CREATE TABLE IF NOT EXISTS event_intake_backoff (id INTEGER PRIMARY KEY CHECK(id=1), until_at INTEGER NOT NULL)')
    def begin(self,now):
        row=self.db.execute('SELECT until_at FROM event_intake_backoff WHERE id=1').fetchone()
        if row and row[0]>now:return False
        self.db.execute('INSERT OR REPLACE INTO event_intake_backoff VALUES(1,?)',(now+900000,))
        return True
    def clear(self):self.db.execute('DELETE FROM event_intake_backoff')

def acknowledge_if_safe(intake,generation,state,backoff):
    if not safe_to_ack(state):return False
    acknowledged=intake.acknowledge(generation).get('acknowledged') is True
    if acknowledged:backoff.clear()
    return acknowledged
