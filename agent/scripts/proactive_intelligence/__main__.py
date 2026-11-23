"""Local reconcile/run CLI. Model invocation requires explicit --reason."""
import argparse
import json
import os
import time
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from .state import State
from .runner import Runner, GatewayPublisher, routines_primary
from .hermes_adapter import HermesReasoner

ROUTINE_LOCK_WAIT_S = 120
ROUTINE_LOCK_POLL_S = 2
BUSY = 'routine-skipped: coordinator busy'
PRIMARY_OFF = ('routines-primary off: OMNI_ROUTINES_PRIMARY is not 1, so the Telegram tweet job owns tweets; '
               'this run publishes nothing to Omni')


def exit_code(receipt):
    """Non-zero when a routine's publication failed: Hermes records the job as failed (Jobs / health)."""
    return 1 if receipt.get('routineFailures') else 0


def ingest_reconciled(state, batch, now, reply_guard=None):
    """The bounded hourly snapshot already coalesces its observed changes."""
    events = list(batch.get('events', []))
    mails=[e for e in events if e.get('active') is not False and e.get('channel')=='email']
    if reply_guard is not None and mails:
        checked=reply_guard.check(mails)
        replied_ids={e['id'] for e in checked['replied']}
        replied_at={e['id']:e['replied_at'] for e in checked['replied'] if e.get('replied_at')}
        unknown_ids={e['id'] for e in checked['unknown']}
        events=[dict(e,active=False,resolved=True,invalidation='confirmed_sent_reply',**({'replied_at':replied_at[e['id']]} if e['id'] in replied_at else {})) if e['id'] in replied_ids else dict(e,queued=False) if e['id'] in unknown_ids else e for e in events]
        state.ingest('canonical-replies',None,[],health='unavailable' if unknown_ids else 'healthy',now=now)
    state.ingest(batch['source'], batch.get('cursor'), events,
        batch.get('health', 'unavailable'), now, coalesce_ms=0,
        deadline_updates=batch.get('deadline_updates'))
    return events


def publish_modes(legacy_flag, environ, routine):
    """(cards, legacy). Item runs make cards; `--legacy-publish` or OMNI_PROACTIVE_LEGACY=1 is the
    rollback to the old private-preparation publication. Daily routines stay as they are (Phase 5)."""
    legacy = bool(legacy_flag) or environ.get('OMNI_PROACTIVE_LEGACY') == '1'
    return (not legacy and not routine), legacy


def cards_health(state, now, window_ms=86400000):
    """Small health block for the job receipt: cards runs that had no vault tools, and named run refusals."""
    since = now - window_ms
    return {'cardsRunsWithoutVaultTools': state.cards_runs_without_vault_tools(since), 'runRefusals': state.run_refusals(since),
            'runFailures': state.run_failures(since), 'holdFailures': state.hold_failures(since)}


def _main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path(os.environ.get('OMNI_PROACTIVE_STATE', str(Path(__file__).resolve().parents[2] / 'state' / 'proactive.sqlite3'))))
    parser.add_argument('--routine', choices=['morning','evening','tweet','nightly-review'], help='one source-grounded daily synthesis using the shared budget')
    parser.add_argument('--event-intake', action='store_true', help='admit only a ready durable content-free wake signal')
    parser.add_argument('--anchor-id', default=os.environ.get('OMNI_PROACTIVE_ANCHOR_ID'))
    parser.add_argument('--reconcile', action='store_true', help='read source changes with deterministic detectors')
    parser.add_argument('--input', type=Path, help='JSON list of {source,cursor,events,health} batches')
    parser.add_argument('--context', type=Path, help='preassembled active context; no nested reasoning')
    parser.add_argument('--publish-shadow', help='explicitly publish one still-fresh recorded shadow run without another model call')
    parser.add_argument('--reason', action='store_true', help='invoke bounded Hermes once if eligible')
    parser.add_argument('--show-decisions', action='store_true', help='print sensitive decision content; default receipts contain only counts')
    parser.add_argument('--live', action='store_true', help='publish validated decisions to local Omni gateway')
    parser.add_argument('--hermes-path', default=os.environ.get('HERMES_SOURCE'))
    parser.add_argument('--hermes-python', default=os.environ.get('HERMES_PYTHON'))
    parser.add_argument('--model', default=os.environ.get('OMNI_PROACTIVE_MODEL'))
    parser.add_argument('--provider', default=os.environ.get('OMNI_PROACTIVE_PROVIDER'))
    parser.add_argument('--base-url', default=os.environ.get('OMNI_PROACTIVE_BASE_URL'))
    parser.add_argument('--legacy-publish', action='store_true', help='publish preparations the old way (rollback switch; also OMNI_PROACTIVE_LEGACY=1)')
    parser.add_argument('--gateway-url', default='http://127.0.0.1:8787')
    args = parser.parse_args()
    if args.routine == 'tweet' and args.live and not routines_primary():
        # One switch for deploy and rollback: with the flag off the Telegram job is the only tweet pipeline.
        print(json.dumps({'wakeAgent': False, 'mode': 'routines-primary-off', 'routine': 'tweet', 'publications': 0, 'note': PRIMARY_OFF}))
        return
    cards_mode, legacy = publish_modes(args.legacy_publish, os.environ, args.routine)
    if args.live and not (args.reason or args.publish_shadow): parser.error('--live requires --reason or --publish-shadow')
    if args.publish_shadow and not args.live: parser.error('--publish-shadow requires --live')
    if args.event_intake and (not args.reconcile or not args.reason or not args.live or args.routine): parser.error('--event-intake requires --reconcile --reason --live and no routine')
    from .intake import Intake, Backoff, acknowledge_if_safe
    intake = Intake(args.gateway_url,os.environ.get('OMNI_SERVICE_TOKEN')) if args.event_intake else None
    generation = intake.ready(int(time.time()*1000)) if intake else None
    if intake and generation is None:
        print(json.dumps({'wakeAgent':False,'mode':'event-idle'})); return
    args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = State(args.state)
    backoff = Backoff(state) if intake else None
    if backoff and not backoff.begin(int(time.time()*1000)):
        print(json.dumps({'wakeAgent':False,'mode':'event-backoff'})); return
    from .replies import ReplyGuard
    reply_guard = ReplyGuard()
    from .source_health import SourceHealthGuard, fresh_bridge_health
    service_token=os.environ.get('OMNI_SERVICE_TOKEN')
    health_publisher=GatewayPublisher(args.gateway_url,service_token) if service_token else None
    source_health_guard=SourceHealthGuard(fetch=health_publisher.source_health if health_publisher else None, fetch_bridge=fresh_bridge_health)
    source_health_guard.refresh()
    state.ingest('ingestion:matrix',None,[],health='healthy' if source_health_guard.matrix['healthy'] else 'unavailable',now=int(time.time()*1000))
    now = int(time.time() * 1000)
    resolution_events = []
    try:
        from proactive_management import Management
        management = Management(args.state)
    except ImportError:
        management = None
    inbound = None
    if args.reconcile:
        from parachute_client import ParachuteClient
        from .detectors import collect
        from .scheduling import VaultInbound
        inbound = VaultInbound(ParachuteClient())
        for batch in collect(ParachuteClient(), now, management=management,source_health_guard=source_health_guard,on_rows=inbound.load):
            reconciled_events=ingest_reconciled(state,batch,now,reply_guard=reply_guard)
            resolution_events.extend(e for e in reconciled_events if e.get('resolved') is True and e.get('active') is False)
    focus_boundary = None
    if args.routine:
        from parachute_client import ParachuteClient
        from .routines import build
        routine_event = build(ParachuteClient(), args.routine, now, args.anchor_id)
        if args.routine == 'tweet':
            from .routines import RECENT_DRAFTS
            routine_event['recent_drafts'] = state.recent_drafts('tweet', RECENT_DRAFTS)
        state.ingest('routine:' + args.routine, routine_event['boundary'], [routine_event], now=now, coalesce_ms=0)
        focus_boundary = routine_event['boundary']
    if args.input:
        for batch in json.loads(args.input.read_text()):
            resolution_events.extend(e for e in batch.get('events', []) if e.get('resolved') is True and e.get('active') is False)
            state.ingest(batch['source'], batch.get('cursor'), batch.get('events', []), batch.get('health', 'unknown') if 'health' in batch else 'healthy', now, deadline_updates=batch.get('deadline_updates'))
    publisher = None
    if args.live:
        token = os.environ.get('OMNI_SERVICE_TOKEN')
        if not token: parser.error('--live requires OMNI_SERVICE_TOKEN in environment')
        from parachute_client import ParachuteClient
        from .routines import person_names
        publisher = GatewayPublisher(args.gateway_url, token, legacy=legacy, names=lambda: person_names(ParachuteClient()))
        if management is not None:
            publisher.sync_preferences(management.preference_snapshot())
        tracked = {json.loads(row[0])['event']['id'] for row in state.db.execute("SELECT payload FROM outbox WHERE status IN ('published','unknown')")}
        resolutions = [{'sourceId': e['id'], 'sourceRevision': e['revision']} for e in resolution_events if e['id'] in tracked]
        for offset in range(0, len(resolutions), 100):
            publisher.resolve_sources(resolutions[offset:offset+100])
    scheduler = None
    if args.live and cards_mode and not args.routine and not legacy:
        # Holds for proposed times: placed at publish, swept every tick (card state from the gateway, replies from
        # the index this run's detectors read; without a reconcile pass the reply check is skipped as unknown).
        from .scheduling import Scheduler
        scheduler = Scheduler(state, fetch_card_state=publisher.card_state, fetch_approval_state=publisher.approval_state,
                              inbound_since=inbound.inbound_since if inbound is not None else (lambda ref, since: None))
    if args.publish_shadow:
        print(json.dumps(Runner(state, None, publisher, reply_guard=reply_guard,source_health_guard=source_health_guard,scheduler=scheduler).publish_shadow(args.publish_shadow)))
        return
    if not args.reason:
        queued = state.db.execute("SELECT count(*) FROM queue WHERE status='queued'").fetchone()[0]
        print(json.dumps({'wakeAgent': False, 'queued': queued, 'mode': 'shadow', 'sources': [dict(r) for r in state.db.execute('SELECT name,health,checked FROM sources')]})); return
    if not args.hermes_path or not args.model: parser.error('--reason requires configured --hermes-path and --model')
    reasoner = HermesReasoner(args.hermes_path, args.model, args.provider, args.base_url, hermes_python=args.hermes_python)
    context_health = 'provided' if args.context else 'unavailable'
    context = args.context.read_text() if args.context else ''
    if (args.reconcile or args.routine) and not args.context:
        try:
            from standing_agent.active_packet import assemble_active_context
            context = assemble_active_context()
            context_health = 'healthy'
        except Exception:
            context = 'Active context unavailable; reason only from supplied evidence. Do not infer missing facts.'
    if management is not None and hasattr(management, 'preferences'):
        context = 'Explicit owner preferences: ' + json.dumps(management.preferences()) + '\n' + context
    context = 'Active context health: ' + context_health + '\n' + context
    assembler = None
    if args.reconcile or args.routine:
        from .context import ContextAssembler, gog_calendar
        client = ParachuteClient()
        if cards_mode:
            # The owner's own addresses: standing_agent.relevance.SELF_ADDRESSES (shared with the nudge sweep).
            from standing_agent.relevance import SELF_ADDRESSES
            assembler = ContextAssembler(client, state, people_notes=lambda: client.query_notes(tag='person', limit=5000, include_content=False, strict_response=True),
                                         calendar=gog_calendar, self_addresses=SELF_ADDRESSES)
        else:
            assembler = ContextAssembler(client, state)
    receipt = Runner(state, reasoner, publisher, context_assembler=assembler, reply_guard=reply_guard,source_health_guard=source_health_guard,cards=cards_mode,scheduler=scheduler).tick(context, live=args.live, focus_boundary=focus_boundary, routine=args.routine)
    if not args.show_decisions and 'decisions' in receipt:
        receipt['decisionCount'] = len(receipt.pop('decisions'))
    receipt['sourceHealth']={'matrix':source_health_guard.matrix}
    receipt['cards'] = cards_mode
    receipt['health'] = cards_health(state, int(time.time() * 1000))
    if intake:
        receipt['intakeAcknowledged'] = acknowledge_if_safe(intake,generation,state,backoff)
    print(json.dumps(receipt))
    code = exit_code(receipt)
    if code:
        for why in receipt.get('routineFailureReasons') or ['routine publication failed']:
            print('routine-failed: ' + why, file=sys.stderr)
        sys.exit(code)


def main():
    # Hermes' cron worker PATH lacks Homebrew/system dirs: docker, security and gog live there.
    import system_path
    system_path.ensure()
    # All invocations serialize collection, model reservation and publication.
    from .intake import coordinator_lock, coordinator_lock_wait, record_busy, BUSY_FAIL_AFTER
    pre=argparse.ArgumentParser(add_help=False)
    pre.add_argument('--state',type=Path,default=Path(os.environ.get('OMNI_PROACTIVE_STATE',str(Path(__file__).resolve().parents[2]/'state'/'proactive.sqlite3'))))
    pre.add_argument('--routine')
    parsed,_=pre.parse_known_args()
    if parsed.routine:
        # A daily routine runs once: it waits for the events job instead of skipping silently, and a
        # run that still cannot start fails visibly (non-zero exit -> the Hermes job's last_status).
        with coordinator_lock_wait(parsed.state,ROUTINE_LOCK_WAIT_S,ROUTINE_LOCK_POLL_S) as acquired:
            if not acquired:
                print(json.dumps({'wakeAgent':False,'mode':'coordinator-busy','error':BUSY}))
                print(BUSY,file=sys.stderr)
                sys.exit(1)
            _main()
        return
    with coordinator_lock(parsed.state) as acquired:
        streak=record_busy(parsed.state,not acquired)
        if not acquired:
            # A skip is normal while another run works; a long streak means a wedged holder that could
            # not be reclaimed, so fail visibly instead of reporting `ok` every minute.
            print(json.dumps({'wakeAgent':False,'mode':'coordinator-busy','consecutiveBusy':streak}))
            if streak>=BUSY_FAIL_AFTER:
                print('coordinator busy for %d consecutive runs; lock holder is not finishing'%streak,file=sys.stderr)
                sys.exit(1)
            return
        _main()

if __name__ == '__main__': main()
