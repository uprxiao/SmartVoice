from __future__ import annotations

import asyncio
import os
import hashlib
import logging
import secrets
import datetime as dt
from pathlib import Path
from contextlib import asynccontextmanager, nullcontext

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import null, select, text
from sqlalchemy.exc import SQLAlchemyError
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from .api import (
    admin,
    admin_providers,
    auth,
    capabilities,
    dictionary,
    history as history_router,
    insertions,
    learning,
    sessions,
    settings,
    users,
)
from .api.deps import ApiError
from .api.sessions import websocket_session
from .core.config import load_config
from .core.db import Database
from .core.http_guard import AdmissionMetrics, HttpGuard
from .core.security import hash_password
from .services.model_call_recorder import DatabaseCallRecorder
from .services.provider_manager import ProviderManager
from .services.provider_service import ProviderService
from .services import stats_service, learning_service
from .services.recovery_snapshot import (
    RecoveryOwnershipLost, SessionRecoveryIdentity, capture_recovery_snapshot,
)

logger = logging.getLogger("smartvoiceinput.main")
from .services.retention import content_expired
from .providers import build_asr, build_llm
from .services.capacity import CapacityError, CapacityService
from .services.cleanup import cleanup_loop, mark_restarted_pending
from .services.classifier import ClassifierService
from .services.rate_limit import FixedWindowRateLimiter, LoginRateLimiter
from .services.refiner import RefinerService
from .services.session_runtime import SessionManager


def _error_response(
    status: int,
    code: str,
    message: str,
    request_id: str,
    retryable=False,
    details=None,
    headers=None,
):
    return JSONResponse(
        status_code=status,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "retryable": bool(retryable),
                "details": details or {},
            }
        },
    )


async def _verify_schema(db):
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    alembic = AlembicConfig()
    alembic.set_main_option("script_location", str(migrations))
    expected = set(ScriptDirectory.from_config(alembic).get_heads())
    if not expected:
        raise RuntimeError("SCHEMA_HEAD_MISSING")
    async with db.engine.connect() as connection:
        actual = set(await connection.run_sync(lambda conn: MigrationContext.configure(conn).get_current_heads()))
    if actual != expected:
        raise RuntimeError("SCHEMA_REVISION_MISMATCH")


async def _recover_interrupted_sessions(db, *, identities=None):
    from .models import SessionAttempt, SessionResult, User, VoiceSession
    from .models.session import ACTIVE_STATES
    now = dt.datetime.now(dt.timezone.utc)
    if identities is None:
        # Retain the explicit direct-maintenance entry point. Startup always
        # supplies its earlier ownership-bound tuple, even when that is empty.
        async with db.session_factory() as session:
            identities = tuple(SessionRecoveryIdentity(*row) for row in (await session.execute(select(
                VoiceSession.user_id, VoiceSession.id, VoiceSession.current_attempt_no,
                VoiceSession.state, VoiceSession.retry_cache_status,
            ).where((VoiceSession.state.in_(ACTIVE_STATES)) |
                    (VoiceSession.retry_cache_status == "available")))).all())
    by_user = {}
    for identity in sorted(set(identities)):
        by_user.setdefault(identity.user_id, []).append(identity)
    for user_id, selected in sorted(by_user.items()):
        async with db.session_factory() as transaction:
            user = await transaction.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None:
                continue
            rows = []
            for identity in selected:
                session = await transaction.scalar(select(VoiceSession).where(
                    VoiceSession.user_id == user_id, VoiceSession.id == identity.resource_id,
                ).with_for_update())
                # Recheck after locking: never recover a new attempt or a
                # successor's changed state/cache using an older snapshot.
                if session is not None and (
                    session.current_attempt_no, session.state, session.retry_cache_status
                ) == (identity.attempt_no, identity.state, identity.retry_cache_status) and (
                    session.state in ACTIVE_STATES or session.retry_cache_status == "available"
                ):
                    rows.append(session)
            for session in rows:
                session.retry_cache_status = "server_restarted"
                if session.state not in ACTIVE_STATES:
                    session.asr_hints, session.llm_hints = null(), null()
                    continue
                attempt = await transaction.scalar(select(SessionAttempt).where(
                    SessionAttempt.user_id == user_id, SessionAttempt.session_id == session.id,
                    SessionAttempt.attempt_no == session.current_attempt_no))
                raw = attempt.raw_text if attempt else None
                if session.state == "refining" and raw and not content_expired(session, user):
                    chars = sum(ch.isalnum() for ch in raw)
                    result = SessionResult(user_id=user_id, session_id=session.id,
                        attempt_no=session.current_attempt_no, raw_text=raw, refined_text=raw,
                        review_required=True, review_reason="server_restarted",
                        text_hash=hashlib.sha256(raw.encode()).hexdigest(), dictionary_version=session.dictionary_version,
                        char_count=chars, stop_to_result_ms=0)
                    transaction.add(result)
                    await transaction.flush()
                    session.state, session.current_result_id, session.error_code = "ready", result.id, None
                    session.retry_cache_status = "none"
                    await stats_service.record_session(transaction, session, "ready", has_result=True,
                                                        char_count=chars, fallback=True)
                    if attempt:
                        attempt.state = "ready"
                else:
                    session.state, session.error_code = "failed", "SERVER_RESTARTED"
                    session.current_result_id = None
                    session.asr_hints, session.llm_hints = null(), null()
                    await stats_service.record_session(transaction, session, "failed")
                    if attempt:
                        attempt.state, attempt.raw_text = "failed", None
                session.first_terminal_at = session.first_terminal_at or now
                if attempt:
                    attempt.error_code, attempt.ended_at = "SERVER_RESTARTED", now
            await transaction.commit()


async def _instance_lock_alive(connection, key: int, backend_pid: int, *, connection_mutex=None):
    if connection is None or connection.closed or connection.invalidated:
        return False
    unsigned = key % (1 << 64)
    query = text("""select pg_backend_pid() = :pid and exists(
        select 1 from pg_locks where locktype='advisory' and granted
        and pid=pg_backend_pid() and classid::bigint=:high and objid::bigint=:low and objsubid=1)""")
    async with asyncio.timeout(2):
        # Queueing behind startup's snapshot is part of the original 2s budget.
        async with (connection_mutex if connection_mutex is not None else nullcontext()):
            if connection.closed or connection.invalidated:
                return False
            alive = bool(await connection.scalar(query, {"pid": backend_pid, "high": unsigned >> 32, "low": unsigned & 0xffffffff}))
            await connection.commit()
            return alive


def _cancel_owned_tasks(*tasks):
    """Request cancellation once; a second cancel may interrupt DB rollback."""
    current = asyncio.current_task()
    owned = tuple(dict.fromkeys(task for task in tasks if task is not None and task is not current))
    for task in owned:
        if not task.done() and not task.cancelling():
            task.cancel()
    return owned


async def _fence_instance(app):
    state = app.state
    state.instance_fenced, state.ready, state.degraded = True, False, True
    state.unavailable_reason = "INSTANCE_LOCK_LOST"
    tasks = _cancel_owned_tasks(
        state.startup_task, state.probe_task, state.cleanup_task, state.llm_prepare_task,
    )
    manager = state.sessions
    if manager:
        # Upgrade the same close owner immediately. This never becomes graceful
        # merely because normal shutdown was already draining completed audio.
        manager.begin_close(graceful=False, code="INTERNAL_ERROR")
    learning_service.begin_shutdown(app, abort=True)
    if tasks:
        await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
    engine = getattr(state.asr, "engine", None)
    if engine:
        await asyncio.gather(*(worker.stop() for worker in engine.workers), return_exceptions=True)
    # Do not await state.shutdown_task: it keeps this watcher alive until the
    # DB/model owners finish, then joins the watcher before releasing ownership.
    closers = [learning_service.close(app, abort=True)]
    if manager:
        closers.append(manager.close())
    results = await asyncio.gather(*closers, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            logger.error("Instance fence cleanup incomplete: %s", type(result).__name__)


async def _watch_instance_lock(app, connection, backend_pid):
    while not app.state.instance_fenced:
        await asyncio.sleep(1)
        if app.state.instance_fenced:
            return
        try:
            alive = await _instance_lock_alive(connection, app.state.config.server.instance_lock_key, backend_pid,
                                               connection_mutex=app.state.instance_connection_mutex)
        except Exception:
            alive = False
        if not alive:
            await _fence_instance(app)
            return  # Never silently acquire a new ownership epoch.


async def _refresh_readiness(app):
    state, config = app.state, app.state.config
    if state.instance_fenced or getattr(state, "stopping", False) or not state.schema_ok:
        state.ready = False
        return
    try:
        async with asyncio.timeout(2):
            async with state.db.session_factory() as db:
                from .models import RuntimeCapacity
                await db.scalar(text("select 1"))
                capacity_row = await db.get(RuntimeCapacity, 1)
                if capacity_row is None:
                    raise RuntimeError("CAPACITY_CONFIG_MISSING")
                current = capacity_row.max_active_sessions
        state.database_ok = True
    except Exception:
        state.database_ok, state.ready = False, False
        state.unavailable_reason = "INSTANCE_LOCK_LOST" if state.instance_fenced else "SERVER_SHUTDOWN" if state.stopping else "DATABASE_UNAVAILABLE"
        return
    if state.instance_fenced or state.stopping:
        return
    if state.asr is None:
        try:
            # V2 §7.2: admin-selected provider first, YAML fallback with a reason.
            asr = await state.providers.startup("asr")
            state.asr = asr
            asr.recorder = state.recorder
            await asr.prepare()
            if state.instance_fenced or state.stopping:
                return
            if config.asr.stable_partial_required and not getattr(asr.capabilities, "stable_partial", False):
                raise RuntimeError("ASR_STABLE_PARTIAL_UNAVAILABLE")
            state.asr_prepared = True
        except Exception:
            logger.exception("ASR 准备失败，readiness 将保持未就绪")
            state.asr_prepared = False
            if state.asr:
                try:
                    await state.asr.close()
                except Exception:
                    pass
            state.asr = None
    if state.instance_fenced or state.stopping:
        return
    if state.llm is None:
        try:
            state.llm = await state.providers.startup("llm")
        except Exception as exc:
            # Configuration failures must identify the component without
            # including URLs, credentials or arbitrary exception messages.
            logger.error("Unsupported or invalid LLM provider configuration (%s)", type(exc).__name__)
            raise
        state.llm.recorder = state.recorder
    # LLM warm-up is optional for readiness. A broken remote LLM must not
    # keep ASR unavailable or prevent a capacity correction taking effect.
    if not state.llm_prepared and (state.llm_prepare_task is None or state.llm_prepare_task.done()):
        async def prepare_llm():
            try:
                async with asyncio.timeout(10):
                    await state.llm.prepare()
                state.llm_prepared = not state.instance_fenced and not state.stopping
            except Exception:
                state.llm_prepared = False
        state.llm_prepare_task = asyncio.create_task(prepare_llm())
    async def healthy(provider):
        if provider is None:
            return False
        try:
            async with asyncio.timeout(2):
                return bool(await provider.health())
        except Exception:
            return False
    asr_ok, llm_ok = await asyncio.gather(healthy(state.asr), healthy(state.llm))
    if state.instance_fenced or state.stopping:
        return
    state.asr_available = state.asr_prepared and asr_ok
    state.llm_available = state.llm_prepared and llm_ok
    if state.sessions is None:
        # Read-only history/output and insertion receipts stay available while a
        # model is unavailable; new recordings are still rejected by the
        # readiness gate in HttpGuard. ASR is only touched when a session opens.
        state.classifier = ClassifierService(state.llm, budget_ms=config.pipeline.classification_budget_ms,
                                            automatic_retries=config.llm.automatic_retries_per_attempt)
        state.sessions = SessionManager(config, state.db, state.capacity,
            RefinerService(state.llm, budget_ms=config.pipeline.stop_to_result_budget_ms,
                           automatic_retries=config.llm.automatic_retries_per_attempt,
                           edits_only_candidate=config.llm.refine_protocol == 'legacy_edits',
                           protocol=config.llm.refine_protocol), state.asr)
    elif state.asr_prepared:
        # A failed initial prepare still creates the manager for history access.
        # Rebind its factory before publishing ready after a successful retry;
        # existing runtimes retain their own ASR session and manager identity.
        state.sessions.asr_provider = state.asr
    over_ceiling = current > config.effective_admission_ceiling
    state.ready = bool(state.database_ok and state.asr_available and not over_ceiling and not state.instance_fenced and not state.stopping)
    state.degraded = not state.ready or not state.llm_available
    state.unavailable_reason = ("INSTANCE_LOCK_LOST" if state.instance_fenced else "SERVER_SHUTDOWN" if state.stopping else
        "CAPACITY_CONFIG_INVALID" if over_ceiling else "ASR_UNAVAILABLE" if not state.asr_available else
        "LLM_UNAVAILABLE" if not state.llm_available else None)


async def _readiness_loop(app):
    while True:
        await asyncio.sleep(2)
        try:
            await _refresh_readiness(app)
        except Exception:
            app.state.ready, app.state.degraded = False, True
            app.state.unavailable_reason = ("INSTANCE_LOCK_LOST" if app.state.instance_fenced else
                                            "SERVER_SHUTDOWN" if app.state.stopping else "DEPENDENCY_UNAVAILABLE")


async def _startup_instance(app, lock_conn, backend_pid, *, skip_lock):
    """One owned task for startup work that may hold independent DB sessions."""
    state = app.state
    if state.instance_fenced:
        raise asyncio.CancelledError
    await _verify_schema(state.db)
    state.schema_ok = state.database_ok = True
    try:
        if skip_lock:
            # Only the lifespan's explicit development skip-lock path gets this
            # mode. It freezes identities but makes no ownership guarantee.
            async with state.db.engine.connect() as connection:
                snapshot = await capture_recovery_snapshot(connection, require_lock=False)
        else:
            async with state.instance_connection_mutex:
                snapshot = await capture_recovery_snapshot(lock_conn,
                    backend_pid=backend_pid, key=state.config.server.instance_lock_key)
    except RecoveryOwnershipLost:
        # No recovery identity can be published on a query/commit failure. This
        # child may also be the first observer of loss, before the next heartbeat.
        await _fence_instance(app)
        raise asyncio.CancelledError
    try:
        await state.capacity.ensure_initialized()
    except CapacityError:
        if state.instance_fenced:
            raise asyncio.CancelledError
        # Keep the existing DB/auth/admin recovery plane for a capacity fix.
        state.unavailable_reason = "CAPACITY_CONFIG_INVALID"
    # Per-user commits completed before fencing remain committed. Cancelling
    # this task stops the next phase and rolls back its current transaction.
    await stats_service.rebuild_all_usage_daily(state.db)
    await state.recorder.rebuild_usage_before_recovery()
    await mark_restarted_pending(app, identities=snapshot.learning)
    await _recover_interrupted_sessions(state.db, identities=snapshot.sessions)
    await state.recorder.recover_incomplete(identities=snapshot.calls)
    if state.instance_fenced or state.stopping:
        raise asyncio.CancelledError
    # Only committed recovery opens the HTTP management plane. Model loading
    # remains owned by this same task and can now expose STARTING over HTTP.
    state.startup_recovery_complete.set_result(None)
    await _refresh_readiness(app)
    if not state.instance_fenced and not state.stopping:
        # Never race the initial prepare with the periodic probe, or register
        # new maintenance after shutdown/fencing has taken its task snapshot.
        state.cleanup_task = asyncio.create_task(cleanup_loop(app))
        state.probe_task = asyncio.create_task(_readiness_loop(app))


def _startup_finished(app, task):
    """Retrieve failures even when lifespan has already passed the DB barrier."""
    error = asyncio.CancelledError() if task.cancelled() else task.exception()
    if error is None:
        return
    state = app.state
    if state.instance_fenced or state.stopping:
        return  # Their synchronous cutoff already published the stronger reason.
    state.ready, state.degraded = False, True
    reason = str(error)
    state.unavailable_reason = (reason if reason in {
        "SCHEMA_HEAD_MISSING", "SCHEMA_REVISION_MISMATCH"
    } else "STARTUP_DEPENDENCY_UNAVAILABLE")
    if not state.schema_ok:
        state.database_ok = False
    logger.error("Startup task did not complete: %s", type(error).__name__)


def _begin_instance_shutdown(app):
    """Publish one cutoff before transport closes; share the original drain owner."""
    state = app.state
    state.stopping, state.ready, state.degraded = True, False, True
    if not state.instance_fenced:
        state.unavailable_reason = "SERVER_SHUTDOWN"
    graceful = not state.instance_fenced
    if state.sessions:
        state.sessions.begin_close(graceful=graceful,
            code="SERVER_SHUTDOWN" if graceful else "INTERNAL_ERROR")
    learning_service.begin_shutdown(app, abort=not graceful)
    if state.drain_task is None:
        tasks = _cancel_owned_tasks(
            state.startup_task, state.probe_task, state.cleanup_task, state.llm_prepare_task,
        )
        state.drain_task = asyncio.create_task(_drain_instance_work(app, tasks, graceful))
    return state.drain_task


async def _drain_instance_work(app, tasks, graceful):
    state = app.state
    # The lock watcher remains live throughout this join. Loss upgrades the
    # existing manager/learning owners to abort, without cancelling their join.
    closers = [learning_service.close(app, abort=not graceful)]
    if state.sessions:
        closers.append(state.sessions.close(graceful=graceful))
    results = await asyncio.gather(*tasks, *closers, return_exceptions=True)
    # Startup/probe errors have already established degraded readiness.
    failures = [result for result in results[len(tasks):] if isinstance(result, Exception)]
    requests = tuple(state.active_requests)
    if requests:
        await asyncio.gather(*requests)
    return failures


async def _close_instance_resources(app, lock_conn, lock_task):
    state, config = app.state, app.state.config
    failures = []
    try:
        failures.extend(await asyncio.shield(_begin_instance_shutdown(app)))
    finally:
        # Only after consumers and their DB writes have ended may the monitor
        # stop. A watcher already fencing owns cleanup; never second-cancel it.
        if lock_task is not None:
            if not state.instance_fenced:
                _cancel_owned_tasks(lock_task)
            results = await asyncio.gather(lock_task, return_exceptions=True)
            failures.extend(result for result in results if isinstance(result, Exception))
        try:
            for provider in (state.asr, state.llm):
                if provider:
                    try:
                        await provider.close()
                    except Exception as exc:
                        failures.append(exc)
        finally:
            try:
                if lock_conn:
                    try:
                        await lock_conn.scalar(text("select pg_advisory_unlock(:key)"),
                                               {"key": config.server.instance_lock_key})
                        await lock_conn.commit()
                    except Exception:
                        # An already lost advisory connection cannot be unlocked.
                        pass
                    await lock_conn.close()
            finally:
                await state.db.close()
    if failures:
        for failure in failures:
            logger.error("Shutdown cleanup incomplete: %s", type(failure).__name__)
        raise RuntimeError("SHUTDOWN_CLEANUP_INCOMPLETE") from failures[0]


def create_app(config_path: str | None = None) -> FastAPI:
    config = load_config(config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = app.state
        state.db = Database(config)
        state.config = config
        state.ready = state.schema_ok = state.database_ok = False
        state.degraded, state.instance_fenced = True, False
        state.stopping = False
        state.active_requests = set()
        state.correction_requests = set()
        state.background_tasks = set()
        state.learning_jobs = {}
        state.learning_closing = state.learning_abort = state.learning_cleanup_failed = False
        state.shutdown_learning_events = set()
        state.learning_close_task = None
        state.asr = state.llm = state.sessions = state.classifier = None
        state.asr_prepared = state.llm_prepared = state.asr_available = state.llm_available = False
        state.startup_task = state.probe_task = state.cleanup_task = state.shutdown_task = None
        state.drain_task = None
        state.startup_recovery_complete = asyncio.get_running_loop().create_future()
        state.instance_connection_mutex = asyncio.Lock()
        state.llm_prepare_task = None
        state.unavailable_reason = "STARTING"
        state.capacity = CapacityService(config, state.db)
        state.recorder = DatabaseCallRecorder(state.db)
        state.providers = ProviderManager(state, config, yaml_asr=lambda c: build_asr(c),
                                          yaml_llm=lambda c: build_llm(c))
        state.provider_service = ProviderService(state, state.providers)
        state.login_limiter = LoginRateLimiter()
        state.register_limiter = FixedWindowRateLimiter(5, 3600)
        state.refresh_limiter = FixedWindowRateLimiter(60, 60)
        state.authenticated_limiter = FixedWindowRateLimiter(120, 60)
        state.admission_metrics = AdmissionMetrics()
        state.password_slots = asyncio.Semaphore(config.auth.max_parallel_hashes)
        # No automatically trusted resource identity exists in this version.
        # A deployment attestation hook may inject one; reports cannot set it.
        state.trusted_environment_fingerprint = getattr(state, "trusted_environment_fingerprint", None)
        lock_conn, lock_task, pid = None, None, None
        # The cleanup owner covers startup too: parent cancellation before yield
        # must not strand the advisory connection or a maintenance transaction.
        try:
            state.dummy_password_hash = await asyncio.to_thread(hash_password, config.auth, secrets.token_urlsafe(32))
            try:
                if any(int(os.environ.get(name, "1")) != 1 for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS")):
                    raise RuntimeError("MULTIPLE_WORKERS_UNSUPPORTED")
                skip_lock = os.environ.get("SVI_INSTANCE_LOCK", "1") == "0"
                if skip_lock and config.environment == "production":
                    raise RuntimeError("INSTANCE_LOCK_REQUIRED")
                if not skip_lock:
                    lock_conn = await state.db.engine.connect()
                    acquired = await lock_conn.scalar(text("select pg_try_advisory_lock(:key)"),
                                                      {"key": config.server.instance_lock_key})
                    if not acquired:
                        state.instance_fenced = True
                        raise RuntimeError("INSTANCE_LOCK_UNAVAILABLE")
                    pid = await lock_conn.scalar(text("select pg_backend_pid()"))
                    await lock_conn.commit()
                    lock_task = asyncio.create_task(_watch_instance_lock(app, lock_conn, pid))
                # No await separates watcher creation from ownership of this
                # child, so an observed loss always has a task it can cancel.
                state.startup_task = asyncio.create_task(_startup_instance(app, lock_conn, pid, skip_lock=skip_lock))
                state.startup_task.add_done_callback(lambda task: _startup_finished(app, task))
                try:
                    # A failed/cancelled child before recovery must not leave
                    # lifespan waiting forever on a success-only barrier.
                    await asyncio.wait((state.startup_task, state.startup_recovery_complete),
                                       return_when=asyncio.FIRST_COMPLETED)
                    if state.startup_task.done():
                        await asyncio.shield(state.startup_task)
                except asyncio.CancelledError:
                    parent = asyncio.current_task()
                    if (not state.instance_fenced or not state.startup_task.cancelled()
                            or (parent is not None and parent.cancelling())):
                        raise
                    # Only the fenced child's cancellation is a degraded start.
                    # An external lifespan cancellation still propagates.
                    state.ready, state.degraded = False, True
                    state.unavailable_reason = "INSTANCE_LOCK_LOST"
            except Exception as exc:
                state.ready, state.degraded = False, True
                reason = str(exc)
                if reason in {"MULTIPLE_WORKERS_UNSUPPORTED", "INSTANCE_LOCK_REQUIRED", "INSTANCE_LOCK_UNAVAILABLE"}:
                    raise
                state.unavailable_reason = ("INSTANCE_LOCK_LOST" if state.instance_fenced else
                    reason if reason in {"SCHEMA_HEAD_MISSING", "SCHEMA_REVISION_MISMATCH"}
                    else "STARTUP_DEPENDENCY_UNAVAILABLE")
                if not state.schema_ok:
                    state.database_ok = False
            yield
        finally:
            state.shutdown_task = asyncio.create_task(_close_instance_resources(app, lock_conn, lock_task))
            cancelled_during_cleanup = False
            # Keep one cleanup owner. Repeated parent cancellation must not
            # cancel a rollback or worker process join half way through.
            while not state.shutdown_task.done():
                try:
                    await asyncio.shield(state.shutdown_task)
                except asyncio.CancelledError:
                    cancelled_during_cleanup = True
            await state.shutdown_task
            if cancelled_during_cleanup:
                raise asyncio.CancelledError

    app = FastAPI(
        title="SmartVoiceInput",
        version="1.0.0",
        docs_url="/docs" if config.environment == "development" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.config = config

    app.add_middleware(HttpGuard, owner=app)
    if config.security.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.security.cors_origins,
            allow_credentials=False,
            # PUT: V2 `PUT /api/admin/providers/active` (design §8.1).
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-Id", "Idempotency-Key"],
            expose_headers=["X-Request-Id"],
            # Admin is served from a different localhost port during local
            # development; Chrome requires this for the private-network preflight.
            allow_private_network=True,
        )

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return _error_response(
            exc.status,
            exc.code,
            exc.message,
            getattr(request.state, "request_id", ""),
            retryable=exc.retryable,
            details=exc.details,
            headers=exc.headers,
        )

    @app.exception_handler(CapacityError)
    async def capacity_unavailable_handler(request: Request, exc: CapacityError):
        request.app.state.ready = False
        return _error_response(503, "SERVICE_UNAVAILABLE", "容量配置暂时不可用",
                               getattr(request.state, "request_id", ""), retryable=True)

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(request: Request, exc: SQLAlchemyError):
        request.app.state.database_ok = request.app.state.ready = False
        request.app.state.unavailable_reason = ("INSTANCE_LOCK_LOST" if request.app.state.instance_fenced else
            "SERVER_SHUTDOWN" if request.app.state.stopping else "DATABASE_UNAVAILABLE")
        return _error_response(503, "SERVICE_UNAVAILABLE", "数据服务暂时不可用",
                               getattr(request.state, "request_id", ""), retryable=True)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        if any(item.get("type") == "json_invalid" for item in errors):
            return _error_response(
                400,
                "INVALID_JSON",
                "请求体不是合法的 JSON",
                getattr(request.state, "request_id", ""),
            )
        return _error_response(
            422,
            "VALIDATION_ERROR",
            "请求参数无效",
            getattr(request.state, "request_id", ""),
            details={
                "fields": [
                    {
                        "type": item.get("type"),
                        "loc": list(item.get("loc", ())),
                        "msg": item.get("msg"),
                    }
                    for item in exc.errors()
                ]
            },
        )

    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(settings.router)
    app.include_router(capabilities.router)
    app.include_router(sessions.router)
    app.include_router(dictionary.router)
    app.include_router(insertions.router)
    app.include_router(learning.router)
    app.include_router(history_router.router)
    app.include_router(admin.router)
    app.include_router(admin_providers.router)

    @app.websocket("/ws/v1/sessions/{session_id}/audio")
    async def ws_session(websocket: WebSocket, session_id: str):
        await websocket_session(websocket, session_id)

    @app.get("/health/live")
    async def health_live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def health_ready(request: Request):
        state = request.app.state
        if not state.ready:
            return JSONResponse(status_code=503, content={"status": "not_ready",
                "reason_code": state.unavailable_reason or "SERVICE_UNAVAILABLE"})
        return {"status": "ready" if state.llm_available else "degraded", "dependencies": {
            "database": "ok", "asr": "ready", "llm": "ready" if state.llm_available else "unavailable"}}

    @app.get("/healthz")
    async def healthz(request: Request):
        state = request.app.state
        ready = bool(state.ready and not state.instance_fenced)
        return JSONResponse(status_code=200 if ready else 503, content={
            "status": "ok" if ready else "degraded", "ready": ready,
            "database": state.database_ok, "degraded": state.degraded or not ready,
            "unavailable_reason": state.unavailable_reason,
            # V2 §9: provider source (db/yaml + reason) per kind; no addresses/keys.
            "providers": {kind: {"source": src.source, "reason": src.reason}
                          for kind, src in getattr(getattr(state, "providers", None), "sources", {}).items()}})

    return app


app = create_app()


def create_server(application: FastAPI, **overrides):
    """The supported Uvicorn entry preserves HTTP/WS shutdown ordering."""
    import uvicorn
    from .core.server import ManagedServer

    config = application.state.config
    options = dict(host=config.server.host, port=config.server.port, workers=1,
        # Keep the pinned backend's existing HTTP-denial workaround.
        ws="websockets", proxy_headers=False, access_log=False)
    options.update(overrides)
    return ManagedServer(uvicorn.Config(application, **options),
        begin_shutdown=lambda: _begin_instance_shutdown(application))


def main() -> None:
    create_server(app).run()


if __name__ == "__main__":
    main()
