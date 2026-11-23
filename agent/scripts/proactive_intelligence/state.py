"""Durable incremental attention queue. All timestamps are epoch milliseconds."""
import hashlib
import os
import json
import sqlite3
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: the owner's IANA timezone (identity owner.timezone)


# Non-routine runs are spread across the day: at most 2 per local hour, 20 per day. Routine families (tweet,
# nightly-review, ...) have their own small daily cap. The morning/evening briefings are owned by the Telegram
# composers (6422b38) and hold no slots.
NONROUTINE_HOURLY_LIMIT = 2
NONROUTINE_DAILY_LIMIT = 20
ROUTINE_DAILY_LIMIT = 2
# The only routine families with an ordinary daily slot. A stray morning/evening (composer-owned) or any other
# routine run gets none, so it can never take the tweet's slot.
BUDGETED_ROUTINES = ('tweet', 'nightly-review')
URGENT_DAILY_LIMIT = 2

def key(event):
    return hashlib.sha256(json.dumps([event['id'], event['revision'], event.get('boundary', '')]).encode()).hexdigest()


#: Failed or lost runs by reason (omni_health reads the same two queries).
RUN_FAILURES_SQL = ("SELECT CASE WHEN status='unknown' THEN 'lost' ELSE COALESCE(json_extract(result,'$.reason'),"
                    "json_extract(result,'$.refusal'),'other') END, count(*) FROM runs WHERE status IN ('failed','unknown') "
                    "AND started>=? GROUP BY 1")
#: Tool calls outside the allowlist that a finished run answered with a tool error (hermes_adapter, usage.refusedTools).
REFUSED_TOOLS_SQL = ("SELECT COALESCE(sum(json_extract(usage,'$.refusedTools')),0) FROM runs WHERE status='done' AND started>=? "
                     "AND json_valid(usage) AND json_extract(usage,'$.refusedTools')>0")

class State:
    def __init__(self, path):
        if str(path) != ':memory:':
            descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            os.chmod(str(path), 0o600)
            for suffix in ('-wal', '-shm'):
                if os.path.exists(str(path) + suffix): os.chmod(str(path) + suffix, 0o600)
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS detector_deadlines(id TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS sources(name TEXT PRIMARY KEY,cursor TEXT,health TEXT,checked INTEGER);
        CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,revision TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS queue(key TEXT PRIMARY KEY,source TEXT,payload TEXT,status TEXT,ready INTEGER,attempts INTEGER DEFAULT 0,run TEXT);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,day TEXT,bucket TEXT,status TEXT,started INTEGER,until_at INTEGER,usage TEXT,result TEXT);
        CREATE TABLE IF NOT EXISTS watchers(id TEXT PRIMARY KEY,source_id TEXT,revision TEXT,kind TEXT,at INTEGER,status TEXT);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,payload TEXT,status TEXT,attempts INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS dismissals(entity TEXT PRIMARY KEY,revision TEXT);
        CREATE TABLE IF NOT EXISTS scheduling_log(at INTEGER,kind TEXT,proposal TEXT,detail TEXT);
        ''')
        if 'routine' not in {r[1] for r in self.db.execute('PRAGMA table_info(runs)')}:
            self.db.execute('ALTER TABLE runs ADD COLUMN routine TEXT')

    def ingest(self, source, cursor, events, health='healthy', now=0, coalesce_ms=180000, deadline_updates=None):
        if health not in ('healthy', 'partial', 'unavailable'):
            raise ValueError('invalid source health')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for event in events:
                for field in ('id', 'revision', 'kind', 'entity', 'occurred_at', 'content', 'url'):
                    if field not in event:
                        raise ValueError('missing evidence ' + field)
                payload = json.dumps(event, sort_keys=True)
                if event.get('active') is False:
                    self.db.execute("UPDATE queue SET status='stale' WHERE json_extract(payload,'$.id')=? AND status IN ('queued','running')", (event['id'],))
                    if event.get('resolved') is True:
                        self.db.execute("UPDATE watchers SET status='canceled' WHERE source_id=? AND status='waiting'", (event['id'],))
                prior = self.db.execute('SELECT revision FROM evidence WHERE id=?', (event['id'],)).fetchone()
                revision_watches=[]
                if prior and prior['revision']!=event['revision'] and event.get('resolved') is not True:
                    revision_watches=self.db.execute("SELECT * FROM watchers WHERE source_id=? AND kind='revision' AND revision<>? AND status='waiting'",(event['id'],event['revision'])).fetchall()
                    for watch in revision_watches:
                        wake=dict(event,active=True,watch_wake=True,boundary='watch:'+watch['id'])
                        self.db.execute('INSERT OR IGNORE INTO queue(key,source,payload,status,ready) VALUES(?,?,?,?,?)',(key(wake),'watcher',json.dumps(wake),'queued',now))
                    if revision_watches:
                        event=dict(event,watch_wake=True);payload=json.dumps(event,sort_keys=True)
                self.db.execute('INSERT OR REPLACE INTO evidence VALUES(?,?,?)', (event['id'], event['revision'], payload))
                # Same-revision metadata enrichments (channel/health/body) must reach pending work.
                self.db.execute("UPDATE queue SET payload=? WHERE key=? AND status='queued'",(payload,key(event)))
                if prior and prior['revision'] != event['revision']:
                    self.db.execute("UPDATE queue SET status='stale' WHERE json_extract(payload,'$.id')=? AND json_extract(payload,'$.revision')<>? AND status IN ('queued','running')", (event['id'],event['revision']))
                    self.db.execute("UPDATE watchers SET status='fired' WHERE source_id=? AND kind='revision' AND revision<>? AND status='waiting'", (event['id'], event['revision']))
                    self.db.execute("UPDATE watchers SET revision=? WHERE source_id=? AND kind='time' AND status='waiting'", (event['revision'],event['id']))
                if event.get('active') is not False and event.get('queued') is not False and not self.dismissed(event):
                    self.db.execute("UPDATE queue SET status='coalesced' WHERE status='queued' AND json_extract(payload,'$.id')=? AND key<>?", (event['id'], key(event)))
                    self.db.execute('INSERT OR IGNORE INTO queue(key,source,payload,status,ready) VALUES(?,?,?,?,?)', (key(event), source, payload, 'queued', now if event.get('urgent') else now + coalesce_ms))
            if health == 'healthy':
                for source_id, value in (deadline_updates or {}).items():
                    self.db.execute('INSERT OR REPLACE INTO detector_deadlines VALUES(?,?)', (source_id, value))
            # Cursor and durable queue commit together, even for partially healthy sources.
            self.db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?)', (source, json.dumps(cursor), health, now))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def fresh(self, evidence):
        return all((row := self.db.execute('SELECT revision,payload FROM evidence WHERE id=?', (ref['sourceId'],)).fetchone()) and row['revision'] == ref['revision'] and json.loads(row['payload']).get('resolved') is not True and (json.loads(row['payload']).get('active') is not False or json.loads(row['payload']).get('watch_wake') is True) for ref in evidence)

    def dismissed(self, event):
        """An owner dismissal of this item at this revision. Routine runs (boundary routine:<name>:<day>) are
        addressed by a stable routing anchor, not an item: a dismissal recorded for that note never silences them."""
        if event.get('routine'): return False
        row = self.db.execute('SELECT revision FROM dismissals WHERE entity=?', (event['entity'],)).fetchone()
        return bool(row) and row['revision'] == event['revision']

    def dismiss(self, entity, revision):
        # Dismissals apply to items only: pending/queued routine runs on the same anchor note are left alone.
        self.db.execute('INSERT OR REPLACE INTO dismissals VALUES(?,?)', (entity, revision))
        self.db.execute("UPDATE outbox SET status='suppressed' WHERE json_extract(payload,'$.event.entity')=? AND json_extract(payload,'$.event.routine') IS NULL AND status='pending'",(entity,))
        self.db.execute("UPDATE queue SET status='dismissed' WHERE json_extract(payload,'$.entity')=? AND json_extract(payload,'$.routine') IS NULL AND status IN ('queued','running')", (entity,))

    def add_watcher(self, source_id, revision, kind='revision', at=None):
        if kind not in ('revision', 'time') or (kind == 'time' and at is None):
            raise ValueError('invalid watcher')
        if kind=='time':
            existing=self.db.execute("SELECT id FROM watchers WHERE source_id=? AND kind='time' AND at=? AND status='waiting'",(source_id,at)).fetchone()
            if existing:
                self.db.execute('UPDATE watchers SET revision=? WHERE id=?',(revision,existing['id']));return existing['id']
        wid = hashlib.sha256(json.dumps([source_id, revision, kind, at]).encode()).hexdigest()
        self.db.execute('INSERT OR IGNORE INTO watchers VALUES(?,?,?,?,?,?)', (wid, source_id, revision, kind, at, 'waiting'))
        return wid

    def preflight_events(self, now, boundary=None):
        rows=self.db.execute("SELECT payload FROM queue WHERE status='queued' AND ready<=? AND attempts<3 AND source NOT IN (SELECT name FROM sources WHERE health<>'healthy') AND (? IS NULL OR json_extract(payload,'$.boundary')=?) ORDER BY CASE WHEN json_extract(payload,'$.urgent')=1 THEN 0 ELSE 1 END, COALESCE(json_extract(payload,'$.deadline'),9223372036854775807), COALESCE(json_extract(payload,'$.score'),0) DESC, json_extract(payload,'$.occurred_at') DESC,ready,key",(now,boundary,boundary)).fetchall()
        events=[json.loads(row['payload']) for row in rows]
        if boundary is None:
            for watch in self.db.execute("SELECT * FROM watchers WHERE status='waiting' AND kind='time' AND at<=?",(now,)):
                row=self.db.execute('SELECT payload FROM evidence WHERE id=? AND revision=?',(watch['source_id'],watch['revision'])).fetchone()
                if row:
                    event=json.loads(row['payload']); event['boundary']='watch:'+watch['id'];events.append(event)
        selected=[];seen=set();chars=0
        for event in events:
            size=len(json.dumps(event))
            if event['id'] in seen or selected and chars+size>9000:continue
            selected.append(event);seen.add(event['id']);chars+=size
            if len(selected)==10:break
        return selected

    def invalidate(self, source_id, reason='replied', at=None):
        """`at`: when his reply was sent (epoch ms), when the Sent evidence says; kept on the evidence row."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT payload FROM evidence WHERE id=?',(source_id,)).fetchone()
            if row:
                event=json.loads(row['payload']);event['active']=False;event['resolved']=True;event['invalidation']=reason
                if isinstance(at,int) and not isinstance(at,bool):event['replied_at']=at
                self.db.execute('UPDATE evidence SET payload=? WHERE id=?',(json.dumps(event),source_id))
            self.db.execute("UPDATE queue SET status='stale' WHERE json_extract(payload,'$.id')=? AND status IN ('queued','running')",(source_id,))
            self.db.execute("UPDATE watchers SET status='canceled' WHERE source_id=? AND status='waiting'",(source_id,))
            self.db.execute("UPDATE outbox SET status='stale' WHERE json_extract(payload,'$.event.id')=? AND status='pending'",(source_id,))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise

    def reserve(self, now, lease_ms=900000, boundary=None, allowed_ids=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            # Unknown outcomes stay charged. Expired runs retry at most three times.
            self.db.execute("UPDATE queue SET status=CASE WHEN attempts<3 THEN 'queued' ELSE 'failed' END,run=NULL WHERE status='running' AND run IN (SELECT id FROM runs WHERE status='running' AND until_at<=?)", (now,))
            self.db.execute("UPDATE runs SET status='unknown' WHERE status='running' AND until_at<=?", (now,))
            if self.db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                self.db.execute('COMMIT'); return None
            for watch in self.db.execute("SELECT * FROM watchers WHERE status='waiting' AND kind='time' AND at<=?", (now,)).fetchall():
                row = self.db.execute('SELECT payload FROM evidence WHERE id=? AND revision=?', (watch['source_id'], watch['revision'])).fetchone()
                if row:
                    event = json.loads(row['payload']); event['boundary'] = 'watch:' + watch['id'];event['active']=True;event['watch_wake']=True
                    self.db.execute('UPDATE evidence SET payload=? WHERE id=?',(json.dumps(event),event['id']))
                    self.db.execute('INSERT OR IGNORE INTO queue(key,source,payload,status,ready) VALUES(?,?,?,?,?)', (key(event), 'watcher', json.dumps(event), 'queued', now))
                self.db.execute("UPDATE watchers SET status='fired' WHERE id=?", (watch['id'],))
            allowed_json = json.dumps(allowed_ids) if allowed_ids is not None else None
            rows = self.db.execute("SELECT * FROM queue WHERE status='queued' AND ready<=? AND attempts<3 AND source NOT IN (SELECT name FROM sources WHERE health<>'healthy') AND (? IS NULL OR json_extract(payload,'$.boundary')=?) AND (? IS NULL OR json_extract(payload,'$.id') IN (SELECT value FROM json_each(?))) ORDER BY CASE WHEN json_extract(payload,'$.urgent')=1 THEN 0 ELSE 1 END, COALESCE(json_extract(payload,'$.deadline'),9223372036854775807), COALESCE(json_extract(payload,'$.score'),0) DESC, json_extract(payload,'$.occurred_at') DESC,ready,key", (now, boundary, boundary, allowed_json, allowed_json)).fetchall()
            day = datetime.fromtimestamp(now / 1000, ZoneInfo(OWNER_TZ)).date().isoformat()
            counts = {r['bucket']: r['n'] for r in self.db.execute('SELECT bucket,count(*) n FROM runs WHERE day=? GROUP BY bucket', (day,))}
            denver = ZoneInfo(OWNER_TZ)
            hour = datetime.fromtimestamp(now / 1000, denver).strftime('%Y-%m-%dT%H')
            started = [r[0] for r in self.db.execute("SELECT started FROM runs WHERE day=? AND bucket='ordinary' AND routine IS NULL", (day,))]
            nonroutine_count = len(started)
            hourly_count = sum(datetime.fromtimestamp(t / 1000, denver).strftime('%Y-%m-%dT%H') == hour for t in started)
            routine_count = self.db.execute("SELECT count(*) FROM runs WHERE day=? AND bucket='ordinary' AND routine IN (%s)" % ','.join('?' * len(BUDGETED_ROUTINES)), (day, *BUDGETED_ROUTINES)).fetchone()[0]
            selected_bucket=None
            selected_family=None
            selected = []
            packet_chars = 0
            selected_ids = set()
            for row in rows:
                event = json.loads(row['payload'])
                urgent = event.get('urgent') is True and isinstance(event.get('deadline'), (int, float)) and now <= event['deadline'] <= now + 3600000
                if event['id'] in selected_ids: continue
                if selected and packet_chars + len(row['payload']) > 9000: continue
                family=event.get('routine')
                can_ordinary=(family in BUDGETED_ROUTINES and routine_count < ROUTINE_DAILY_LIMIT) if family is not None else hourly_count < NONROUTINE_HOURLY_LIMIT and nonroutine_count < NONROUTINE_DAILY_LIMIT
                event_bucket='ordinary' if can_ordinary else 'urgent' if urgent and counts.get('urgent',0)<URGENT_DAILY_LIMIT else None
                if selected and (family!=selected_family or event_bucket!=selected_bucket):continue
                if event_bucket is not None:
                    selected_bucket=event_bucket
                    selected_family=family
                    selected.append(row)
                    selected_ids.add(event['id'])
                    packet_chars += len(row['payload'])
                    if len(selected) == 10: break
            if not selected:
                self.db.execute('COMMIT'); return None
            run = uuid.uuid4().hex
            bucket = selected_bucket
            self.db.execute('INSERT INTO runs(id,day,bucket,status,started,until_at,usage,result,routine) VALUES(?,?,?,?,?,?,?,?,?)', (run, day, bucket, 'running', now, now + lease_ms, None, None, selected_family))
            for row in selected:
                self.db.execute("UPDATE queue SET status='running',attempts=attempts+1,run=? WHERE key=?", (run, row['key']))
            self.db.execute('COMMIT')
            return run, [json.loads(r['payload']) for r in selected]
        except BaseException:
            self.db.execute('ROLLBACK'); raise

    def cards_runs_without_vault_tools(self, since=0):
        """Completed cards runs whose Hermes exposed no read-only vault tool (health: 'cards ran without vault tools')."""
        return self.db.execute("SELECT count(*) FROM runs WHERE status='done' AND started>=? AND json_extract(result,'$.vaultTools')='unavailable'", (since,)).fetchone()[0]

    def recent_drafts(self, routine, limit):
        """The drafts of the latest finished runs of one routine, newest first (empty drafts skipped)."""
        rows = self.db.execute("SELECT json_extract(result,'$.decisions[0].draft') FROM runs WHERE routine=? AND status='done' "
                               "ORDER BY started DESC LIMIT ?", (routine, limit * 2))
        return [r[0].strip() for r in rows if isinstance(r[0], str) and r[0].strip()][:limit]

    def run_refusals(self, since=0):
        """Failed understanding runs by named refusal (timeout / ToolPolicyError / MemoryGuardError), for health."""
        return {r[0]: r[1] for r in self.db.execute("SELECT json_extract(result,'$.refusal'),count(*) FROM runs WHERE status='failed' AND started>=? AND json_extract(result,'$.refusal') IS NOT NULL GROUP BY 1", (since,))}

    def run_failures(self, since=0):
        """Proactive runs that failed or were refused since `since`, by reason (runner.refusal_reason; older rows fall
        back to their refusal name), plus tool calls refused inside runs that finished (usage.refusedTools, counted
        as tool-outside-allowlist). The same predicate as omni_health._failures."""
        out = {}
        for reason, n in self.db.execute(RUN_FAILURES_SQL, (since,)):
            out[reason] = out.get(reason, 0) + n
        refused = self.db.execute(REFUSED_TOOLS_SQL, (since,)).fetchone()[0] or 0
        if refused:
            out['tool-outside-allowlist'] = out.get('tool-outside-allowlist', 0) + refused
        return out

    def log_scheduling(self, at, kind, proposal=None, detail=''):
        """One line per hold placed, released, confirmed or failed (scheduling.Scheduler.log): the audit trail and the
        hold-failure count health reads."""
        self.db.execute('INSERT INTO scheduling_log VALUES(?,?,?,?)', (int(at), kind, proposal, str(detail or '')[:300]))

    def hold_failures(self, since=0):
        """Hold failures since `since` (epoch ms): any '*-failed' scheduling line, plus releases the calendar could not confirm."""
        return self.db.execute("SELECT count(*) FROM scheduling_log WHERE at>=? AND (kind LIKE '%-failed' OR kind='release-inconclusive')", (since,)).fetchone()[0]

    def scheduling_counts(self, since=0):
        return {r[0]: r[1] for r in self.db.execute('SELECT kind,count(*) FROM scheduling_log WHERE at>=? GROUP BY kind', (since,))}

    def finish(self, run, status, result=None, usage=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('UPDATE runs SET status=?,result=?,usage=? WHERE id=?', (status, json.dumps(result), json.dumps(usage), run))
            if status == 'done' and result and result.get('mode') == 'live':
                for decision in result.get('decisions', []):
                    event = next(e for e in result['events'] if e['id'] == decision['sourceId'])
                    # A tweet routine always reports back: a draft, or why there is none.
                    if decision['action'] not in ('surface', 'work') and event.get('routine') != 'tweet': continue
                    self.db.execute('INSERT OR IGNORE INTO outbox(id,payload,status) VALUES(?,?,?)', (key(event), json.dumps({'decision': decision, 'event': event}), 'pending'))
            target = 'done' if status == 'done' else 'queued'
            self.db.execute("UPDATE queue SET status=CASE WHEN attempts>=3 AND ?='queued' THEN 'failed' ELSE ? END,run=NULL WHERE run=? AND status='running'", (target, target, run))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK'); raise
