"""Opt-in compatibility for Hermes' reschedule-only manual-run HTTP endpoint.

No upstream files are edited. Installation precedes adapter creation; unknown
upstream handler implementations are not replaced. All normal authentication,
job claims, scheduler execution, outcome recording and shutdown tracking remain.
"""
import asyncio
import contextvars
import inspect
import hashlib
import ipaddress
import logging
import os
import re

logger = logging.getLogger(__name__)
#: Reviewed upstream `_handle_run_job` implementations (sha256 of inspect.getsource) and how
#: each one's request checks are spelled. Both only reschedule the job for the next tick.
#:   split-checks  — hermes-agent 791e2ae325 (2026-08-27): _check_auth/_draining_response/
#:                   _check_jobs_available/_check_job_id
#:   request-guard — v0.23.1 (2026-11-09; origin/main 2026-11-12 is identical): one
#:                   _cron_request_guard(request, need_job_id=True, check_draining=True)
KNOWN_RUN_HANDLERS = {
    '0efb13a9f603eabf270f25d6e5b58a47d41f236e62a96f2fc4111057c5e543c2': 'split-checks',
    '787af72715ff521dbb9db68e48f89dfa42eeffa5d0e11604967b419f3aefd1a8': 'request-guard',
}
THREAD = re.compile(r'^omni_[0-9a-f]{8,64}$')


def _claim(jobs, job_id):
    """The atomic claim `cronjob(action='run')` makes. Where Hermes supports it, claim as a
    MANUAL run: an off-tick run-now that leaves the pending `next_run_at` slot in place (without
    it, v0.23+ advances the schedule and the next regular run is skipped)."""
    claim = jobs.claim_job_for_fire
    try:
        manual = 'manual' in inspect.signature(claim).parameters
    except (TypeError, ValueError):
        manual = False
    if manual:
        return claim(job_id, manual=True, return_job=True)
    return claim(job_id, return_job=True)


def _checks(self, request, prelude):
    """Upstream's own request checks, in upstream's order: (job_id, error_response)."""
    if prelude == 'request-guard':
        return self._cron_request_guard(request, need_job_id=True, check_draining=True)
    for check in (lambda: self._check_auth(request), self._draining_response,
                  self._check_jobs_available):
        error = check()
        if error is not None:
            return None, error
    return self._check_job_id(request)


def make_handler(api, jobs, runner, prelude='split-checks'):
    async def run(self, request):
        job_id, error = _checks(self, request, prelude)
        if error is not None:
            return error
        with api._reserve_pending_api_work(self) as reservation:
            try:
                job = jobs.get_job(job_id)
                if job is None:
                    return api.web.json_response({'error': 'Job not found'}, status=404)
                # Same atomic claim as cronjob(action='run'); never force/resume.
                claimed = _claim(jobs, job_id)
                if not isinstance(claimed, dict):
                    return api.web.json_response({'error': 'Job is paused or already running'}, status=409)
                context = contextvars.copy_context()
                worker = asyncio.get_running_loop().run_in_executor(None, context.run, runner, claimed)
                async def drain_worker():
                    try:
                        return await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        # Cancelling an asyncio waiter cannot stop its thread.
                        # Keep both the task and reservation until actual exit.
                        await asyncio.shield(worker)
                        raise
                task = asyncio.create_task(drain_worker())
                self._background_tasks.add(task)
                reservation['detached'] = True
                task.add_done_callback(self._background_tasks.discard)
                worker.add_done_callback(lambda _: api._release_pending_api_work(self, reservation))
                def completed(done):
                    try:
                        done.result()
                    except BaseException as exc:
                        logger.warning('Omni manual job worker ended: %s', type(exc).__name__)
                task.add_done_callback(completed)
                # This acknowledges dispatch, not successful completion.
                view = dict(claimed)
                view.pop('fire_claim', None)
                view.update(executed=True, execution_mode='background')
                return api.web.json_response({'job': view}, status=202)
            except Exception as exc:
                logger.warning('Omni manual job dispatch unavailable: %s', type(exc).__name__)
                return api.web.json_response({'error': 'Job dispatch unavailable'}, status=503)
    return run


def make_origin(original):
    def origin(self, request):
        result = original(self, request)
        thread = request.headers.get('X-Omni-Thread', '')
        # Existing API-key authentication runs before this method. A marker is
        # accepted only from the local Prism gateway, never a forwarded request.
        try:
            local = ipaddress.ip_address(request.remote or '').is_loopback
        except ValueError:
            local = False
        if local and not any(request.headers.get(k) for k in ('Forwarded', 'X-Forwarded-For', 'X-Real-IP')) and THREAD.fullmatch(thread):
            result.update(platform='api_server', chat_id=thread)
        return result
    return origin


def install():
    if os.environ.get('OMNI_BRIDGE_JOBS_API') != '1':
        return False
    from gateway.platforms import api_server as api
    from cron import jobs
    from tools.cronjob_tools import _run_claimed_job
    cls = api.APIServerAdapter
    if getattr(cls, '_omni_jobs_installed', False):
        return True
    source = inspect.getsource(cls._handle_run_job)
    # Restrict this compatibility shim to reviewed implementations. An upstream
    # change must be reviewed instead of silently replaced on upgrade.
    digest = hashlib.sha256(source.encode()).hexdigest()
    prelude = KNOWN_RUN_HANDLERS.get(digest)
    if prelude is None:
        logger.warning('Omni jobs adapter not installed: upstream handler changed (%s)', digest[:12])
        return False
    for name in ('_reserve_pending_api_work', '_release_pending_api_work'):
        if not callable(getattr(api, name, None)):
            return False
    cls._handle_run_job = make_handler(api, jobs, _run_claimed_job, prelude)
    cls._cron_origin_from_request = make_origin(cls._cron_origin_from_request)
    cls._omni_jobs_installed = True
    logger.warning('Omni jobs adapter active (%s): claimed dispatch and approval-thread origin', prelude)
    return True
