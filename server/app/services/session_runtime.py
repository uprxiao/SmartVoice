from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
import time
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import event, null, select
from sqlalchemy.exc import SQLAlchemyError

from ..core.ws_denial import deny_websocket
from ..core.clock import loop_time
from ..core.wire_time import utc_iso8601
from ..core.config import Config
from ..core.security import idempotency_hash
from ..models import (
    Insertion,
    LoginSession,
    SessionAttempt,
    SessionResult,
    User,
    VoiceSession,
)
from ..providers.asr.base import AsrFinal, AsrOpenOptions, AsrResume, AsrSession, ProviderError
from . import dictionary_service as dictionary_svc
from . import stats_service
from .capacity import CapacityError, CapacityService
from .idempotency import begin
from .refiner import RefinerService, RefineResult
from .inference_context import CallScope, call_scope
from .dictionary_prompt import asr_hints, refine_hints
from .retention import content_expired, content_expires_at
from .runtime_auth_batch import RuntimeAuthBatch, RuntimeAuthKey
from .safe_exception_metadata import log_exception_metadata

# V1 default (180 s). A session's own limit is fixed at creation from the
# current ASR provider and audio.max_duration_seconds (V2 §4.1).
MAX_SAMPLES = 2_880_000
CLOUD_FINISH_CAP_S = 25.0
CLOUD_FINISH_FLOOR_S = 10.0
# T12 (decided 2026-10-28): a cloud ASR retry replays its unfinished audio at the
# catch-up rate, so its ASR budget is replay time + 10 s (no 25 s cap); the
# attempt's overall budget is extended by the same replay time.
CLOUD_RETRY_EXTRA_S = 10.0
ACTIVE_STATES = ("created", "streaming", "finalizing", "refining")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionError(ValueError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}


class ProtocolViolation(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class RetryAudioLease:
    expires_at: float
    deadline: float
    owner: RuntimeSession | None
    status: str = "available"
    timer: asyncio.TimerHandle | None = None
    # Completed-segment text of the failed cloud attempt: same TTL and owner
    # as the PCM, memory only (V2 §3.6/§3.7).
    resume: AsrResume | None = None


@dataclass
class RuntimeSession:
    session_id: str
    user_id: str
    device_id: str
    attempt_no: int
    state: str = "created"
    audio: bytearray = field(default_factory=bytearray)
    samples: int = 0
    last_seq: int = -1
    asr: AsrSession | None = None
    events: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    connected: bool = False
    ended: bool = False
    cancelled: bool = False
    # Stop ingress during the locked cancellation transaction, before commit.
    cancel_pending: bool = False
    error_code: str | None = None
    raw_text: str | None = None
    hint_terms: list[str] = field(default_factory=list)
    refine_terms: list[dict] = field(default_factory=list)
    dictionary_snapshot: list[dict] = field(default_factory=list)
    dictionary_version: int = 0
    created_at: float = field(default_factory=time.monotonic)
    end_accepted_at: float | None = None
    retry_expires_at: float | None = None
    audio_lease: RetryAudioLease | None = None
    short_frame: bool = False
    end_last_seq: int | None = None
    end_total_samples: int | None = None
    last_activity: float = field(default_factory=time.monotonic)
    auth_checked_at: float = 0.0
    auth_task: asyncio.Task | None = None
    ctx: object | None = None
    deadline: float | None = None
    processing_accepted: bool = False
    progress_saved_at: float = 0.0
    stable_text: str = ""
    max_samples: int = MAX_SAMPLES
    asr_caps: object | None = None
    asr_provider_id: str | None = None
    asr_metrics: dict | None = None
    pending_resume: AsrResume | None = None
    replay_retry: bool = False

    def emit(self, event_type: str, **fields) -> None:
        payload = {"type": event_type, "session_id": self.session_id,
                   "_queued_at": time.monotonic(), **fields}
        try:
            self.events.put_nowait(payload)
        except asyncio.QueueFull:
            self.error_code = "CLIENT_BACKPRESSURE"
            self.cancelled = True
            self.cancel_event.set()


class SessionManager:
    def __init__(
        self,
        config: Config,
        db,
        capacity: CapacityService,
        refiner: RefinerService,
        asr_provider,
    ):
        self.config = config
        self.db = db
        self.capacity = capacity
        self.refiner = refiner
        self.asr_provider = asr_provider
        self.runtimes: dict[str, RuntimeSession] = {}
        self.background_tasks: set[asyncio.Task] = set()
        self._admission_lock = asyncio.Lock()
        self._auth_poll_origin = time.monotonic()
        self._auth_reads = RuntimeAuthBatch(db)
        self._closing = False
        self._close_abort = False
        self._close_code_value = "SERVER_SHUTDOWN"
        self._close_task: asyncio.Task | None = None
        self._operations: set[asyncio.Future] = set()
        self._websockets: set[asyncio.Future] = set()
        self._auth_tasks: set[asyncio.Task] = set()
        self._processing_tasks: dict[asyncio.Task, RuntimeSession] = {}
        self._closing_runtimes: dict[int, RuntimeSession] = {}
        self._draining: set[int] = set()
        self._background_failed = False

    @asynccontextmanager
    async def _operation(self):
        if self._closing:
            raise SessionError(503, "SERVICE_UNAVAILABLE", "语音服务正在停止")
        # A completion marker tracks this call, not the caller's whole task.
        # In-process ASGI clients can keep that task alive after the response.
        done = asyncio.get_running_loop().create_future()
        self._operations.add(done)
        try:
            yield
        finally:
            done.set_result(None)
            self._operations.discard(done)

    def _start_watchdog(self, runtime, ctx):
        task = asyncio.create_task(self._watchdog(runtime, ctx))
        runtime.auth_task = task
        self._auth_tasks.add(task)
        task.add_done_callback(self._auth_tasks.discard)

    def _register_runtime(self, runtime):
        self.runtimes[runtime.session_id] = runtime
        if self._closing:
            self._closing_runtimes[id(runtime)] = runtime
            self._fence_runtime(runtime, self._close_code_value)

    def _start_processing(self, runtime, coroutine):
        # A superseded/retired attempt can still own provider cleanup. Keep its
        # task independently of the current session-id map until it is done.
        task = asyncio.create_task(coroutine)
        runtime.task = task
        self._processing_tasks[task] = runtime

        def finished(done):
            self._processing_tasks.pop(done, None)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    self._background_failed = True
                    logging.getLogger(__name__).warning(
                        "Processing cleanup/persistence incomplete: %s", type(error).__name__)

        task.add_done_callback(finished)

    def _arm_audio_lease(self, runtime, expires_at):
        lease = runtime.audio_lease
        if lease is not None:
            # The first failure owns the deadline. Retry never renews it.
            if lease.expires_at != expires_at:
                raise RuntimeError("retry audio deadline changed")
            return lease
        lease = RetryAudioLease(expires_at,
            loop_time() + max(0.0, expires_at - time.time()), runtime)
        runtime.audio_lease = lease
        lease.timer = asyncio.get_running_loop().call_at(
            lease.deadline, self._expire_audio_lease, lease)
        return lease

    def _close_audio_lease(self, runtime, status="released"):
        lease = runtime.audio_lease
        if lease is None or lease.owner is not runtime:
            return
        if lease.status == "available":
            lease.status = status
        if lease.timer is not None:
            lease.timer.cancel()
            lease.timer = None
        lease.owner = None
        lease.resume = None

    def _audio_expired(self, runtime):
        lease = runtime.audio_lease
        if lease is not None:
            return lease.status == "expired" or (lease.status == "available" and (
                loop_time() >= lease.deadline or time.time() >= lease.expires_at))
        return runtime.retry_expires_at is not None and time.time() >= runtime.retry_expires_at

    def _expire_audio_lease(self, lease):
        if lease.status != "available" or lease.owner is None:
            return
        if loop_time() < lease.deadline and time.time() < lease.expires_at:
            # Timer resolution may deliver early; never erase before the cutoff.
            lease.timer = asyncio.get_running_loop().call_at(
                lease.deadline, self._expire_audio_lease, lease)
            return
        self._drop_cache_local(lease.owner, "expired")

    def begin_close(self, *, graceful=False, code="SERVER_SHUTDOWN"):
        """Freeze accepted attempts synchronously; a later fence only upgrades."""
        if not self._closing:
            self._closing = True
            self._close_code_value = code
            for runtime in tuple(self.runtimes.values()):
                self._closing_runtimes[id(runtime)] = runtime
                if (graceful and runtime.processing_accepted and runtime.ended
                        and runtime.state in ("finalizing", "refining")
                        and runtime.deadline is not None):
                    self._draining.add(id(runtime))
        if not graceful:
            self._close_abort = True
            if self._close_code_value != "INTERNAL_ERROR":
                self._close_code_value = code
            self._draining.clear()
        for runtime in tuple(self._closing_runtimes.values()):
            if id(runtime) not in self._draining:
                self._fence_runtime(runtime, self._close_code_value)
                # Failed cached runtimes are not ACTIVE and have no drain work.
                # Their timer/PCM must not wait for another admission's DB join.
                self._close_audio_lease(runtime)
                runtime.audio.clear()
                asr, runtime.asr = runtime.asr, None
                if asr:
                    self._background(asr.cancel())

    def _processing_may_start(self, runtime):
        return not self._closing or (not self._close_abort and id(runtime) in self._draining)

    @asynccontextmanager
    async def _admission_session(self):
        # These transactions already serialize on the shared capacity DB row.
        # Queue locally first so their waiters do not occupy the connection pool
        # needed by live-session authorization and other short transactions.
        async with self._admission_lock:
            if self._closing:
                raise SessionError(503, "SERVICE_UNAVAILABLE", "语音服务正在停止")
            async with self.db.session_factory() as db:
                yield db

    async def _locked_session(self, db, user_id: str, session_id: str):
        user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
        session = await db.scalar(select(VoiceSession).where(
            VoiceSession.user_id == user_id, VoiceSession.id == session_id).with_for_update())
        return user, session

    async def _authorized(self, db, runtime: RuntimeSession, user=None) -> bool:
        ctx = runtime.ctx
        if ctx is None or ctx.expires_at <= utcnow():
            return False
        login = await db.scalar(select(LoginSession).where(
            LoginSession.id == ctx.sid, LoginSession.user_id == runtime.user_id))
        user = user or await db.get(User, runtime.user_id)
        return bool(user and user.status == "active" and login and login.revoked_at is None
                    and login.refresh_expires_at > utcnow())

    async def _admit(self, db, capacity_row):
        try:
            await self.capacity.admit(db, locked_capacity=capacity_row)
        except CapacityError as exc:
            raise SessionError(429, "CAPACITY_EXCEEDED", "服务繁忙，请稍后重试",
                {"reason": "active_limit"}, {"Retry-After": "1"}) from exc

    def _asr_capabilities(self):
        return getattr(self.asr_provider, "capabilities", None)

    def _limit_samples(self, caps=None) -> int:
        """Recording limit a new session gets now (V2 §4.1).

        min(provider limit, deployment ``audio.max_duration_seconds``); local
        providers additionally keep the V1 hard ceiling ``MAX_SAMPLES``.
        """
        caps = caps if caps is not None else self._asr_capabilities()
        provider_limit = getattr(caps, "max_record_seconds", None) or MAX_SAMPLES // 16000
        samples = min(int(provider_limit), self.config.audio.max_duration_seconds) * 16000
        if getattr(caps, "replay_rate", None) is None:
            samples = min(samples, MAX_SAMPLES)
        return samples

    def _limit_seconds(self, caps=None) -> int:
        return self._limit_samples(caps) // 16000

    def _provider_full(self) -> bool:
        caps = self._asr_capabilities()
        limit = getattr(caps, "max_concurrent_sessions", None)
        return limit is not None and getattr(self.asr_provider, "active_sessions", 0) >= limit

    @staticmethod
    def _provider_limit_error() -> SessionError:
        return SessionError(429, "CAPACITY_EXCEEDED", "服务繁忙，请稍后重试",
                            {"reason": "provider_limit"}, {"Retry-After": "1"})

    def _bind_asr(self, runtime, caps) -> None:
        runtime.asr_caps = caps
        runtime.asr_provider_id = (getattr(caps, "provider_id", "") or self.config.asr.provider)[:64]

    @staticmethod
    def _apply_asr_metrics(attempt, runtime) -> None:
        attempt.asr_provider_id = runtime.asr_provider_id
        metrics = runtime.asr_metrics
        if metrics:
            attempt.asr_reconnects = int(metrics.get("reconnects", 0))
            attempt.asr_resend_ms = int(round(float(metrics.get("resend_s", 0)) * 1000))

    @staticmethod
    def _capture_asr(runtime, error: ProviderError | None = None) -> None:
        metrics = getattr(runtime.asr, "metrics", None)
        if callable(metrics):
            runtime.asr_metrics = metrics()
        if error is not None and error.resume is not None and error.resume.segments:
            runtime.pending_resume = error.resume

    async def create(self, ctx, client_request_id: str, learning_enabled: bool):
        async with self._operation():
            return await self._create(ctx, client_request_id, learning_enabled)

    async def _create(self, ctx, client_request_id: str, learning_enabled: bool):
        # The shared capacity row is always first. Recheck idempotency only after
        # acquiring it, so concurrent duplicate requests consume a single slot.
        async with self._admission_session() as db:
            capacity_row = await self.capacity.lock(db)
            user = await db.scalar(select(User).where(User.id == ctx.user.id).with_for_update())
            effective_learning = bool(learning_enabled and user and user.learning_enabled)
            request_hash = idempotency_hash(self.config.idempotency_secret, "session.create", {
                "client_request_id": client_request_id, "mode": "dictation",
                "learning_enabled": bool(learning_enabled),
            })
            # Both lookups run after the capacity/user locks. Share a single
            # round trip on the common new-key path without changing replay
            # priority or the separately locked global-capacity check.
            existing_id, active = (await db.execute(select(
                select(VoiceSession.id).where(
                    VoiceSession.user_id == ctx.user.id,
                    VoiceSession.client_request_id == client_request_id,
                ).limit(1).scalar_subquery().label("existing_session_id"),
                select(VoiceSession.id).where(
                    VoiceSession.user_id == ctx.user.id,
                    VoiceSession.state.in_(ACTIVE_STATES),
                ).limit(1).scalar_subquery().label("active_session_id"),
            ))).one()
            existing = None
            if existing_id is not None:
                existing = await db.scalar(select(VoiceSession).where(
                    VoiceSession.user_id == ctx.user.id,
                    VoiceSession.id == existing_id,
                ))
            if existing:
                if existing.request_hash != request_hash:
                    raise SessionError(409, "IDEMPOTENCY_CONFLICT", "相同请求 ID 对应了不同请求")
                return existing, False
            if not user or user.status != "active":
                raise SessionError(403, "ACCOUNT_DISABLED", "账号不可用")
            if active:
                raise SessionError(409, "ACTIVE_SESSION_EXISTS", "已有进行中的语音会话")
            if self._provider_full():
                raise self._provider_limit_error()
            await self._admit(db, capacity_row)
            caps = self._asr_capabilities()
            limit_samples = self._limit_samples(caps)
            limit_seconds = limit_samples // 16000
            dictionary_state = await dictionary_svc.lock_state(db, user.id)
            snapshot = await dictionary_svc.prompt_snapshot(db, user.id)
            hint_terms = asr_hints(snapshot)
            session = VoiceSession(user_id=user.id, device_id=ctx.device_id,
                created_login_sid=ctx.sid, client_request_id=client_request_id,
                request_hash=request_hash, mode="dictation", state="created",
                current_attempt_no=1, dictionary_version=dictionary_state.version,
                learning_enabled=effective_learning, asr_hints=hint_terms, llm_hints=null(),
                asr_hints_applied=bool(getattr(caps, "supports_hints", True)),
                max_record_seconds=limit_seconds)
            db.add(session)
            await db.flush()
            db.add(SessionAttempt(user_id=user.id, session_id=session.id,
                attempt_no=1, kind="initial", state="running"))
            await db.commit()
            runtime = RuntimeSession(session_id=session.id, user_id=user.id,
                device_id=ctx.device_id, attempt_no=1, ctx=ctx,
                hint_terms=hint_terms, dictionary_snapshot=snapshot,
                dictionary_version=dictionary_state.version, max_samples=limit_samples)
            self._bind_asr(runtime, caps)
            self._register_runtime(runtime)
        # No DB connection or row lock survives provider setup.
        if self._closing:
            raise SessionError(503, "SERVICE_UNAVAILABLE", "语音服务正在停止")
        self._start_watchdog(runtime, ctx)
        try:
            async with asyncio.timeout(self.config.sessions.connect_timeout_ms / 1000):
                runtime.asr = await self.asr_provider.open(AsrOpenOptions(
                    session_id=session.id, user_id=runtime.user_id, attempt_no=1,
                    language=self.config.asr.language, hint_terms=hint_terms, max_samples=runtime.max_samples))
            if runtime.state != "created":
                await self._release_audio(runtime)
            return await self.get(session.id, user.id), True
        except Exception as exc:
            await self._fail(runtime, "ASR_UNAVAILABLE")
            if isinstance(exc, ProviderError) and exc.code == "busy":
                # Lost a race for the provider's last slot after the precheck.
                raise self._provider_limit_error() from exc
            raise SessionError(503, "ASR_UNAVAILABLE", "语音识别服务不可用",
                               {"retryable": True}) from exc

    async def get(self, session_id: str, user_id: str) -> VoiceSession:
        async with self.db.session_factory() as db:
            session = await db.scalar(
                select(VoiceSession).where(
                    VoiceSession.id == session_id, VoiceSession.user_id == user_id
                )
            )
            if not session:
                raise SessionError(404, "NOT_FOUND", "会话不存在")
            return session

    async def output(self, session_id: str, *, caller=None) -> dict:
        async with self.db.session_factory() as db:
            if caller is None:
                session = await db.get(VoiceSession, session_id)
            else:
                session = await db.scalar(select(VoiceSession).where(
                    VoiceSession.id == session_id, VoiceSession.user_id == caller.user.id))
            if not session:
                raise SessionError(404, "NOT_FOUND", "会话不存在")
            user = await db.get(User, session.user_id)
            expired = content_expired(session, user)
            result = None
            if session.state == "ready" and session.current_result_id and not expired:
                row = await db.get(SessionResult, session.current_result_id)
                if row and row.content_cleared_at is None:
                    attempt = await db.scalar(
                        select(SessionAttempt).where(
                            SessionAttempt.user_id == row.user_id,
                            SessionAttempt.session_id == row.session_id,
                            SessionAttempt.attempt_no == row.attempt_no,
                        )
                    )
                    result = {
                        "result_id": row.id,
                        "attempt_no": row.attempt_no,
                        "attempt_kind": attempt.kind if attempt else "initial",
                        "raw_text": row.raw_text or "",
                        "refined_text": row.refined_text or "",
                        "review_required": row.review_required,
                        "review_reason": row.review_reason,
                        "text_hash": row.text_hash,
                        "dictionary_version": row.dictionary_version,
                        "stop_to_result_ms": row.stop_to_result_ms,
                        "created_at": row.created_at,
                    }
            runtime = self.runtimes.get(session_id)
            insertion = await db.scalar(select(Insertion).where(Insertion.user_id == session.user_id,
                Insertion.session_id == session_id).order_by(Insertion.claimed_at.desc()).limit(1))
            retry_kind, retry_expires_at, reason = "none", None, "not_needed"
            if expired:
                reason = "content_expired"
            elif insertion and insertion.status in ("reserved", "inserted", "unverified"):
                reason = {"reserved": "insertion_reserved", "inserted": "already_inserted",
                          "unverified": "insertion_unverified"}[insertion.status]
            elif session.state in ACTIVE_STATES:
                reason = "in_progress"
            elif session.state in ("cancelled", "empty"):
                reason = session.state
            elif session.state == "failed":
                expiry = (session.first_asr_failed_at.timestamp() + self.config.retry.audio_ttl_seconds
                          if session.first_asr_failed_at else None)
                if not session.audio_complete:
                    reason = ("server_restarted" if session.retry_cache_status == "server_restarted"
                              else "incomplete_audio")
                elif expiry is not None and expiry <= time.time():
                    reason = "audio_expired"
                elif session.retry_cache_status == "evicted":
                    reason = "audio_evicted"
                elif runtime and runtime.audio and runtime.retry_expires_at and runtime.retry_expires_at > time.time():
                    if session.received_samples > self._limit_samples():
                        reason = "provider_changed"
                    else:
                        retry_kind, reason = "asr", None
                        retry_expires_at = datetime.fromtimestamp(expiry, timezone.utc)
                else:
                    reason = "server_restarted" if session.retry_cache_status in ("server_restarted", "available") else "not_needed"
            elif session.state == "ready" and result and result["review_required"]:
                retry_kind, reason = "refine", None
            # ASR still consumes the creating device's retained audio. Filter
            # only otherwise-available ASR; preserve global reasons and refine.
            if (retry_kind == "asr" and caller is not None
                    and (caller.client_type != "desktop" or not caller.device_id
                         or caller.device_id != session.device_id)):
                retry_kind, retry_expires_at, reason = "none", None, None
            return {
                "session_id": session.id,
                "mode": session.mode,
                "learning_enabled": session.learning_enabled,
                "state": session.state,
                "current_attempt_no": session.current_attempt_no,
                "current_result_id": session.current_result_id,
                "audio_complete": session.audio_complete,
                "received_samples": session.received_samples,
                "audio_ms": session.received_samples * 1000 // 16000,
                "max_record_seconds": session.max_record_seconds,
                "created_at": session.created_at,
                "first_terminal_at": session.first_terminal_at,
                "content_expires_at": content_expires_at(session, user),
                "content_expired": expired,
                "error_code": session.error_code,
                "result": result,
                "retry_kind": retry_kind,
                "retry_expires_at": retry_expires_at,
                "retry_unavailable_reason": reason,
                "insertion_status": insertion.status if insertion else None,
            }

    async def cancel(self, session_id: str, user_id: str, *, caller=None) -> dict:
        frozen: RuntimeSession | None = None
        runtime: RuntimeSession | None = None
        try:
            async with self.db.session_factory() as db:
                _, session = await self._locked_session(db, user_id, session_id)
                if session is None:
                    raise SessionError(404, "NOT_FOUND", "会话不存在")
                current = self.runtimes.get(session_id)
                if (current is not None and current.user_id == user_id
                        and current.session_id == session_id
                        and current.attempt_no == session.current_attempt_no):
                    runtime = current
                if session.state != "cancelled":
                    if session.state not in ACTIVE_STATES:
                        raise SessionError(409, "SESSION_TERMINAL", "会话已结束")
                    if runtime is not None:
                        # The receiver increments samples before its first await.
                        # Fence new frames before snapshotting the unsaved tail;
                        # the user/session lock already protects result commits.
                        runtime.cancel_pending = True
                        frozen = runtime
                        session.received_samples = max(session.received_samples, runtime.samples)
                    session.state = "cancelled"
                    session.error_code = None
                    session.retry_cache_status = "none"
                    session.asr_hints, session.llm_hints = null(), null()
                    session.first_terminal_at = session.first_terminal_at or utcnow()
                    attempt = await db.scalar(select(SessionAttempt).where(
                        SessionAttempt.user_id == user_id, SessionAttempt.session_id == session_id,
                        SessionAttempt.attempt_no == session.current_attempt_no))
                    if attempt:
                        attempt.state, attempt.ended_at, attempt.raw_text = "cancelled", utcnow(), None
                    await stats_service.record_session(db, session, "cancelled")
                    await db.commit()
        except BaseException:
            if frozen is not None:
                # An unconfirmed cancellation must not resume recording or keep
                # PCM. Existing fail-closed CAS preserves any committed terminal.
                self._fence_runtime(frozen, "AUDIO_INTERRUPTED")
            raise
        if runtime is not None:
            runtime.cancelled, runtime.state = True, "cancelled"
            runtime.cancel_pending = False
            runtime.cancel_event.set()
            await self._stop_processing(runtime)
            await self._release_audio(runtime)
            self._retire(runtime)
        try:
            return await self.output(session_id, caller=caller)
        finally:
            # Do not notify the socket while this operation still needs a DB
            # read: its sender may finish and cancel the receiver immediately.
            if runtime is not None:
                runtime.emit("cancelled")

    async def retry(self, ctx, session_id: str, retry_kind: str, idempotency_key: str):
        async with self._operation():
            return await self._retry(ctx, session_id, retry_kind, idempotency_key)

    async def _retry(self, ctx, session_id: str, retry_kind: str, idempotency_key: str):
        # Waiting for local admission remains cancellable and opens no DB
        # session. Once admitted, one owned task completes the short transaction,
        # releases its locks/connection and hands off the exact new attempt.
        # Losing an HTTP response must not cancel COMMIT or return its PCM to an
        # already superseded attempt. close() joins our outer operation marker.
        admission = AsyncExitStack()
        db = await admission.enter_async_context(self._admission_session())
        owner = asyncio.create_task(self._retry_admitted(
            ctx, session_id, retry_kind, idempotency_key, db, admission),
            name="svi-retry-admission")
        interrupted = None
        while not owner.done():
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError as exc:
                if not owner.cancelled() and interrupted is None:
                    interrupted = exc
            except Exception:
                # Read the sole owner's result below, including a real DB
                # failure. Caller cancellation never turns that into success.
                break
        try:
            result = owner.result()
        except BaseException as exc:
            if interrupted is not None:
                logging.getLogger(__name__).warning(
                    "Retry admission failed after caller cancellation: %s", type(exc).__name__)
                raise interrupted from None
            raise
        if interrupted is not None:
            raise interrupted
        return result

    async def _retry_admitted(self, ctx, session_id, retry_kind, idempotency_key, db, admission):
        replayed = False
        # The stack entered in the caller is now exclusively owned here. No
        # caller-side finally can close the session while COMMIT is in flight.
        async with admission:
            capacity_row = await self.capacity.lock(db)
            user, session = await self._locked_session(db, ctx.user.id, session_id)
            if session is None:
                raise SessionError(404, "NOT_FOUND", "会话不存在")
            # Historical refine consumes persisted text for this account;
            # ASR retry and audio WS remain bound to the creating device.
            if retry_kind != "refine" and session.device_id != ctx.device_id:
                raise SessionError(403, "FORBIDDEN", "该会话属于其他设备")
            replayed, _ = await begin(db, self.config.idempotency_secret, ctx.user.id,
                "session.retry", idempotency_key,
                {"session_id": session_id, "retry_kind": retry_kind}, "session", session_id)
            if not replayed:
                if not user or user.status != "active":
                    raise SessionError(403, "ACCOUNT_DISABLED", "账号不可用")
                if content_expired(session, user):
                    raise SessionError(410, "CONTENT_EXPIRED", "结果正文已过期")
                active = await db.scalar(select(VoiceSession.id).where(
                    VoiceSession.user_id == user.id, VoiceSession.state.in_(ACTIVE_STATES)))
                if active:
                    if active == session_id:
                        # An in-flight ASR retry still loses audio eligibility
                        # at its original D. A fresh key must report expiry,
                        # while replay above and durable-text refine stay intact.
                        if (retry_kind == "asr" and session.state == "finalizing"
                                and session.first_asr_failed_at is not None):
                            runtime = self.runtimes.get(session_id)
                            expiry = (session.first_asr_failed_at.timestamp()
                                      + self.config.retry.audio_ttl_seconds)
                            if expiry <= time.time() or (runtime is not None and self._audio_expired(runtime)):
                                if runtime is not None:
                                    self._drop_cache_local(runtime, "expired")
                                raise SessionError(409, "RETRY_UNAVAILABLE", "音频缓存已过期",
                                                   {"reason": "audio_expired"})
                        raise SessionError(409, "RETRY_UNAVAILABLE", "当前尝试仍在处理中", {"reason": "in_progress"})
                    raise SessionError(409, "ACTIVE_SESSION_EXISTS", "已有进行中的语音会话")
                insertion = await db.scalar(select(Insertion).where(
                    Insertion.user_id == user.id, Insertion.session_id == session_id,
                    Insertion.status.in_(("reserved", "inserted", "unverified"))))
                if insertion:
                    reason = {"reserved": "insertion_reserved", "inserted": "already_inserted",
                              "unverified": "insertion_unverified"}[insertion.status]
                    raise SessionError(409, "RETRY_UNAVAILABLE", "已有插入记录", {"reason": reason})
                runtime = self.runtimes.get(session_id)
                if retry_kind == "asr":
                    if session.state != "failed" or not session.first_asr_failed_at:
                        raise SessionError(409, "RETRY_UNAVAILABLE", "当前状态不能重试 ASR",
                            {"reason": {"cancelled": "cancelled", "empty": "empty",
                                        "ready": "not_needed"}.get(session.state,
                                "server_restarted" if session.retry_cache_status == "server_restarted"
                                else "in_progress")})
                    expiry = session.first_asr_failed_at.timestamp() + self.config.retry.audio_ttl_seconds
                    if expiry <= time.time() or (runtime is not None and self._audio_expired(runtime)):
                        if runtime is not None:
                            self._drop_cache_local(runtime, "expired")
                        raise SessionError(409, "RETRY_UNAVAILABLE", "音频缓存已过期", {"reason": "audio_expired"})
                    if runtime is None or not runtime.audio:
                        reason = "audio_evicted" if session.retry_cache_status == "evicted" else "server_restarted"
                        raise SessionError(409, "RETRY_UNAVAILABLE", "音频缓存不可用", {"reason": reason})
                    if session.received_samples > self._limit_samples():
                        # The current ASR provider cannot take this recording
                        # (e.g. 360 s cloud audio after switching to 180 s local).
                        raise SessionError(409, "RETRY_UNAVAILABLE", "识别服务已变更，请重新录音",
                                           {"reason": "provider_changed"})
                elif retry_kind == "refine":
                    if session.state != "ready" or not session.current_result_id:
                        raise SessionError(409, "RETRY_UNAVAILABLE", "当前状态不能重试整理", {"reason": "not_needed"})
                    result = await db.scalar(select(SessionResult).where(
                        SessionResult.id == session.current_result_id, SessionResult.user_id == user.id))
                    if result is None or result.content_cleared_at is not None:
                        raise SessionError(410, "CONTENT_EXPIRED", "结果正文已过期")
                    if not result.review_required:
                        raise SessionError(409, "RETRY_UNAVAILABLE", "该结果无需重新整理", {"reason": "not_needed"})
                    if runtime is None:
                        runtime = RuntimeSession(session_id=session_id, user_id=user.id,
                            device_id=session.device_id, attempt_no=session.current_attempt_no,
                            samples=session.received_samples, ended=True)
                    runtime.raw_text = result.raw_text or ""
                else:
                    raise SessionError(422, "VALIDATION_ERROR", "未知重试类型")
                await self._admit(db, capacity_row)
                previous = session.current_attempt_no
                session.current_attempt_no += 1
                session.current_result_id = None
                session.state = "finalizing" if retry_kind == "asr" else "refining"
                session.error_code = None
                attempt_started = loop_time()
                db.add(SessionAttempt(user_id=user.id, session_id=session_id,
                    attempt_no=session.current_attempt_no, kind=retry_kind,
                    source_attempt_no=previous, state="running",
                    raw_text=runtime.raw_text if retry_kind == "refine" else None))
                await stats_service.record_session(db, session, "active")
                if retry_kind == "asr" and (expiry <= time.time() or self._audio_expired(runtime)):
                    self._drop_cache_local(runtime, "expired")
                    raise SessionError(409, "RETRY_UNAVAILABLE", "音频缓存已过期", {"reason": "audio_expired"})
                previous_runtime = runtime
                # Each attempt owns an immutable identity/context. A late old
                # callback cannot see its attempt_no mutated into the new one.
                runtime = RuntimeSession(session_id=session_id, user_id=user.id,
                    device_id=session.device_id, attempt_no=session.current_attempt_no,
                    state=session.state, samples=session.received_samples, ended=True, ctx=ctx,
                    dictionary_version=session.dictionary_version,
                    hint_terms=session.asr_hints or [], refine_terms=session.llm_hints or [],
                    # Preserve frozen values while isolating old-attempt cleanup,
                    # including nested alias/context containers.
                    dictionary_snapshot=deepcopy(previous_runtime.dictionary_snapshot) if retry_kind == "asr" else [],
                    raw_text=previous_runtime.raw_text if retry_kind == "refine" else None,
                    retry_expires_at=previous_runtime.retry_expires_at if retry_kind == "asr" else None,
                    max_samples=max(session.received_samples, MAX_SAMPLES))
                if retry_kind == "asr":
                    lease = self._arm_audio_lease(previous_runtime, expiry)
                    runtime.audio_lease, previous_runtime.audio_lease = lease, None
                    lease.owner = runtime
                    runtime.audio, previous_runtime.audio = previous_runtime.audio, bytearray()
                runtime.end_accepted_at = attempt_started
                runtime.deadline = attempt_started + self.config.pipeline.stop_to_result_budget_ms / 1000
                # Publish the PCM owner before commit so the existing absolute
                # retry TTL scan can still see and erase it during this await.
                self._register_runtime(runtime)
                commit_confirmed = False
                root_transaction = db.sync_session.get_transaction()

                def confirm_retry(sync_session):
                    nonlocal commit_confirmed
                    if (commit_confirmed or sync_session.in_nested_transaction()
                            or sync_session.get_transaction() is not root_transaction):
                        return
                    commit_confirmed = True
                    # No I/O or dispatch here: only publish the already frozen
                    # attempt after this exact root COMMIT has been confirmed.
                    if (not self._closing and not runtime.cancelled
                            and runtime.state in ("finalizing", "refining")
                            and self.runtimes.get(session_id) is runtime
                            and (retry_kind != "asr" or (
                                lease.owner is runtime and lease.status == "available"
                                and not self._audio_expired(runtime)))):
                        runtime.processing_accepted = True

                event.listen(db.sync_session, "after_commit", confirm_retry)
                try:
                    await db.commit()
                except BaseException:
                    if commit_confirmed:
                        # The new attempt is durable even when an outer commit
                        # wrapper fails afterwards. Never restore superseded PCM.
                        self._fence_runtime(runtime, "INTERNAL_ERROR")
                        raise
                    # Restore only after a confirmed rollback, and only while
                    # the same lease remains usable. An expired/closed lease
                    # never revives PCM, even if COMMIT or rollback was delayed.
                    restored = False
                    rolled_back = False
                    try:
                        await db.rollback()
                        rolled_back = True
                        if (retry_kind == "asr" and lease.status == "available"
                                and not self._audio_expired(runtime) and not self._closing):
                            # Old cleanup may already have retired its context
                            # while PCM belonged to the uncommitted attempt.
                            # Restore its frozen values from that isolated owner,
                            # never from the user's current dictionary.
                            previous_runtime.dictionary_snapshot = deepcopy(runtime.dictionary_snapshot)
                            previous_runtime.hint_terms = deepcopy(runtime.hint_terms)
                            previous_runtime.refine_terms = deepcopy(runtime.refine_terms)
                            previous_runtime.dictionary_version = runtime.dictionary_version
                            previous_runtime.audio, runtime.audio = runtime.audio, bytearray()
                            previous_runtime.audio_lease, runtime.audio_lease = lease, None
                            lease.owner = previous_runtime
                            restored = True
                    finally:
                        if retry_kind == "asr" and not restored:
                            expired = self._audio_expired(runtime)
                            self._close_audio_lease(runtime, "expired" if expired else "released")
                            runtime.audio.clear()
                            previous_runtime.audio.clear()
                            previous_runtime.retry_expires_at = None
                            if expired:
                                self._background(self._persist_cache_status(previous_runtime, "expired"))
                        if rolled_back:
                            self._register_runtime(previous_runtime)
                        else:
                            self._fence_runtime(runtime, "INTERNAL_ERROR")
                    raise
                finally:
                    event.remove(db.sync_session, "after_commit", confirm_retry)
        if replayed:
            return await self.output(session_id, caller=ctx), False
        if not self._processing_may_start(runtime):
            raise SessionError(503, "SERVICE_UNAVAILABLE", "语音服务正在停止")
        if runtime.auth_task and not runtime.auth_task.done():
            runtime.auth_task.cancel()
            await asyncio.gather(runtime.auth_task, return_exceptions=True)
        if retry_kind == "asr" and self._audio_expired(runtime):
            self._drop_cache_local(runtime, "expired")
            return await self.output(session_id, caller=ctx), True
        if (runtime.cancelled or runtime.state not in ("finalizing", "refining")
                or not runtime.processing_accepted):
            return await self.output(session_id, caller=ctx), True
        self._start_watchdog(runtime, ctx)
        self._start_processing(runtime, self._retry_asr(runtime) if retry_kind == "asr"
                               else self._refine(runtime, runtime.raw_text or "", 0))
        return await self.output(session_id, caller=ctx), True

    async def _retry_asr(self, runtime: RuntimeSession):
        # Never copy all cached PCM into an untracked task argument. Cached PCM,
        # provider windows and the child worker all share the first failure TTL.
        try:
            if runtime.cancelled or runtime.state not in ACTIVE_STATES:
                return
            caps = self._asr_capabilities()
            self._bind_asr(runtime, caps)
            lease = runtime.audio_lease
            resume = lease.resume if lease is not None else None
            if resume is not None and not (
                    getattr(caps, "supports_resume", False)
                    and (resume.provider_id, resume.provider_version)
                    == (getattr(caps, "provider_id", ""), getattr(caps, "provider_version", ""))
                    and resume.end_sample <= runtime.samples):
                resume = None  # provider switched/changed: resend the whole recording
            async with asyncio.timeout_at(self._asr_deadline(runtime)):
                if runtime.asr:
                    await runtime.asr.cancel()
                runtime.asr = await self.asr_provider.open(AsrOpenOptions(
                    session_id=runtime.session_id, user_id=runtime.user_id, attempt_no=runtime.attempt_no,
                    language=self.config.asr.language, hint_terms=runtime.hint_terms,
                    max_samples=runtime.max_samples,
                    outcome_deadline=self._asr_operation_deadline(runtime),
                    eligibility_deadline=self._asr_deadline(runtime), resume=resume))
                # Cloud resume: only audio from the first unfinished segment.
                for start in range(resume.end_sample if resume else 0, runtime.samples, 4000):
                    if self._audio_expired(runtime):
                        self._drop_cache_local(runtime, "expired")
                        return
                    await runtime.asr.feed(bytes(runtime.audio[start * 2:(start + 4000) * 2]), start)
            drain = getattr(runtime.asr, "drain_estimate_s", None)
            if getattr(caps, "replay_rate", None) and callable(drain):
                # T12: extend this attempt by its replay time (still bounded by the
                # failed-audio deadline D through _asr_deadline).
                runtime.replay_retry = True
                runtime.deadline = max(runtime.deadline or 0.0, (runtime.end_accepted_at or loop_time())
                                       + drain() + CLOUD_RETRY_EXTRA_S
                                       + self.config.pipeline.stop_to_result_budget_ms / 1000)
            await self._process(runtime)
        except TimeoutError:
            await self._fail(runtime, "ASR_TIMEOUT")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail(runtime, "ASR_FAILED")

    async def _stop_processing(self, runtime):
        task = runtime.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            if not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _release_audio(self, runtime):
        self._close_audio_lease(runtime)
        runtime.audio.clear()
        runtime.retry_expires_at = None
        if runtime.asr:
            asr, runtime.asr = runtime.asr, None
            await asr.cancel()

    def _retire(self, runtime):
        if runtime.state not in ACTIVE_STATES and not runtime.audio:
            runtime.raw_text = None
            runtime.hint_terms.clear()
            runtime.refine_terms.clear()
            runtime.dictionary_snapshot.clear()
            if not runtime.connected and self.runtimes.get(runtime.session_id) is runtime:
                self.runtimes.pop(runtime.session_id, None)

    async def _cancel_runtime(self, runtime):
        try:
            await self.cancel(runtime.session_id, runtime.user_id)
        except SessionError as exc:
            if exc.code != "SESSION_TERMINAL":
                raise

    async def handle_ws(self, ws, session_id: str, ctx) -> None:
        if self._closing:
            await deny_websocket(ws, 503, "SERVICE_UNAVAILABLE", "语音服务正在停止", retryable=True)
            return
        done = asyncio.get_running_loop().create_future()
        self._websockets.add(done)
        try:
            await self._handle_ws(ws, session_id, ctx)
        finally:
            done.set_result(None)
            self._websockets.discard(done)

    async def _handle_ws(self, ws, session_id: str, ctx) -> None:
        runtime = self.runtimes.get(session_id)
        denial = None
        async with self.db.session_factory() as db:
            _, session = await self._locked_session(db, ctx.user.id, session_id)
            if self._closing:
                denial = (503, "SERVICE_UNAVAILABLE", "语音服务正在停止")
            elif not session:
                denial = (404, "NOT_FOUND", "会话不存在")
            elif session.device_id != ctx.device_id:
                denial = (403, "FORBIDDEN", "该会话属于另一台设备")
            elif runtime is None or session.state not in ("created", "streaming"):
                denial = (409, "SESSION_STATE_CONFLICT", "会话状态不允许建立连接")
            elif runtime.connected:
                denial = (409, "SESSION_STATE_CONFLICT", "会话已有连接")
            else:
                runtime.connected = True
                runtime.ctx = ctx
                runtime.state = "streaming"
                session.state = "streaming"
                await db.commit()
        if self._closing and denial is None:
            runtime.connected = False
            self._fence_runtime(runtime, self._close_code_value)
            denial = (503, "SERVICE_UNAVAILABLE", "语音服务正在停止")
        if denial is not None:
            # Release User/Session row locks before writing the HTTP response.
            await deny_websocket(ws, *denial)
            return
        await ws.accept()
        runtime.last_activity = time.monotonic()
        runtime.auth_checked_at = time.monotonic()
        await ws.send_json(
            {
                "type": "ready",
                "session_id": session_id,
                "attempt_no": runtime.attempt_no,
                "audio": {
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": 16000,
                    "channels": 1,
                    "chunk_samples": 4000,
                    "max_samples": runtime.max_samples,
                },
            }
        )

        async def sender():
            while True:
                event = await runtime.events.get()
                queued_at = event.pop("_queued_at", time.monotonic())
                send_budget = self.config.sessions.unacked_limit_ms / 1000
                if event["type"] == "audio.ack":
                    send_budget -= time.monotonic() - queued_at
                try:
                    async with asyncio.timeout(max(0, send_budget)):
                        await ws.send_json(event)
                except TimeoutError as exc:
                    raise SessionError(503, "CLIENT_BACKPRESSURE", "客户端接收超时") from exc
                if event["type"] in (
                    "result.ready",
                    "cancelled",
                    "session.empty",
                    "error",
                ):
                    return

        async def partials():
            if runtime.asr is None:
                return
            last_stable, last_revision, last_decoded = "", 0, 0
            try:
                async for partial in runtime.asr.partials():
                    if (not partial.stable_text.startswith(last_stable) or
                        partial.revision <= last_revision or
                        not last_decoded <= partial.decoded_samples <= partial.received_samples <= runtime.samples):
                        await self._fail(runtime, "ASR_FAILED")
                        return
                    last_stable = runtime.stable_text = partial.stable_text
                    last_revision, last_decoded = partial.revision, partial.decoded_samples
                    runtime.emit(
                        "asr.partial",
                        revision=partial.revision,
                        stable_text=partial.stable_text,
                        provisional_text=partial.provisional_text,
                        decoded_samples=partial.decoded_samples,
                        received_samples=partial.received_samples,
                    )
            except asyncio.CancelledError:
                raise
            except ProviderError as exc:
                if exc.code == "invalid_output":
                    # The provider has rejected its own partial contract (for
                    # example a regressed committed prefix). It is no longer a
                    # usable stream; do not disguise it as an idle microphone.
                    await self._fail(runtime, "ASR_FAILED")
                    return
                # A partial decode failure is non-fatal: finish still performs
                # the authoritative decode, and its error drives the session.
                return

        async def receiver():
            while True:
                packet = await ws.receive()
                if packet["type"] == "websocket.disconnect":
                    raise asyncio.CancelledError()
                runtime.last_activity = time.monotonic()
                if self._closing and id(runtime) not in self._draining:
                    raise SessionError(503, self._close_code_value, "语音服务正在停止")
                if packet.get("bytes") is not None:
                    await self._accept_frame(runtime, packet["bytes"])
                elif packet.get("text") is not None:
                    if len(packet["text"].encode()) > 4096:
                        raise ProtocolViolation("MESSAGE_TOO_LARGE")
                    try:
                        data = json.loads(packet["text"])
                    except json.JSONDecodeError as exc:
                        raise ProtocolViolation("AUDIO_PROTOCOL_ERROR") from exc
                    if not isinstance(data, dict):
                        raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
                    kind = data.get("type")
                    expected = {"type", "last_seq", "total_samples"} if kind == "audio.end" else {"type"}
                    if set(data) != expected:
                        raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
                    if kind == "audio.end":
                        await self._end(
                            runtime, data.get("last_seq"), data.get("total_samples")
                        )
                    elif kind == "cancel":
                        await self._cancel_runtime(runtime)
                        return
                    else:
                        raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        partial_task = asyncio.create_task(partials())
        # The auth/timeout watchdog belongs to the attempt, not this socket.
        # It must survive audio.end and a post-end socket disconnect.
        try:
            done, _ = await asyncio.wait(
                {send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.cancelled():
                    if (task is recv_task and not runtime.ended and not runtime.cancelled
                            and runtime.state in ACTIVE_STATES):
                        self._fence_runtime(runtime, "AUDIO_INTERRUPTED")
                    continue
                exc = task.exception()
                if task is recv_task and exc is None and runtime.cancelled:
                    await self._flush_terminal(send_task)
                if isinstance(exc, ProtocolViolation):
                    event = await self._fail(runtime, exc.code)
                    if send_task.done():
                        # The sender already returned after a terminal result;
                        # deliver the protocol error directly before closing.
                        if event is not None:
                            try:
                                async with asyncio.timeout(2):
                                    await ws.send_json(event)
                            except Exception:
                                pass
                    else:
                        await self._flush_terminal(send_task)
                elif isinstance(exc, SessionError):
                    event = await self._fail(runtime, exc.code)
                    if send_task.done():
                        if event is not None:
                            try:
                                async with asyncio.timeout(2):
                                    await ws.send_json(event)
                            except Exception:
                                pass
                    else:
                        await self._flush_terminal(send_task)
                elif isinstance(exc, asyncio.CancelledError):
                    if (not runtime.ended and not runtime.cancelled
                            and runtime.state in ACTIVE_STATES):
                        self._fence_runtime(runtime, "AUDIO_INTERRUPTED")
                        await self._flush_terminal(send_task)
        finally:
            # The socket's cancel scope can end before receiver handling runs.
            # Transfer incomplete-upload cleanup before the first cancellable
            # await; existing managed background tasks own the terminal CAS.
            if (not runtime.ended and not runtime.cancelled
                    and runtime.state in ACTIVE_STATES):
                self._fence_runtime(runtime, "AUDIO_INTERRUPTED")
            runtime.connected = False
            for task in (send_task, recv_task, partial_task):
                task.cancel()
            await asyncio.gather(
                send_task,
                recv_task,
                partial_task,
                return_exceptions=True,
            )
            self._retire(runtime)
            try:
                await ws.close(code=self._close_code(runtime))
            except Exception:
                pass

    def _background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        def finished(done):
            self.background_tasks.discard(done)
            if done.cancelled():
                self._background_failed = True
            else:
                error = done.exception()
                if error is not None:
                    self._background_failed = True
                    logging.getLogger(__name__).warning("Background cleanup/persistence incomplete: %s", type(error).__name__)
        task.add_done_callback(finished)

    def _fence_runtime(self, runtime, code):
        """Stop local work before any unavailable database acquisition.

        Persistence still uses the same session/attempt CAS, so a result already
        committed ready is not rolled back. The client must query that outcome;
        an infrastructure error never authorizes an automatic insertion.
        """
        if runtime.state not in ACTIVE_STATES:
            return
        runtime.cancelled = True
        runtime.cancel_event.set()
        runtime.state, runtime.error_code = "failed", code
        self._close_audio_lease(runtime)
        runtime.audio.clear()
        runtime.raw_text = None
        runtime.retry_expires_at = None
        runtime.hint_terms.clear()
        runtime.refine_terms.clear()
        runtime.dictionary_snapshot.clear()
        if runtime.task and not runtime.task.done() and not runtime.task.cancelling():
            runtime.task.cancel()
        asr, runtime.asr = runtime.asr, None
        if asr:
            self._background(asr.cancel())
        # Do not put the terminal error behind stale ACK/partial backlog.
        while not runtime.events.empty():
            runtime.events.get_nowait()
        runtime.emit("error", attempt_no=runtime.attempt_no,
            error={"code": code, "message": code, "retryable": False, "details": {}})
        self._background(self._fail(runtime, code, retain_audio=False))

    async def _watchdog(self, runtime: RuntimeSession, ctx) -> None:
        poll = min(1.0, self.config.sessions.auth_poll_ms / 1000)
        while runtime.state in ACTIVE_STATES:
            # Align live runtimes to one manager clock: the initial check may
            # happen sooner, never later than the original poll interval.
            now = time.monotonic()
            tick = int(max(0.0, now - self._auth_poll_origin) / poll) + 1
            next_poll = self._auth_poll_origin + tick * poll
            if next_poll <= now:  # protect against floating-point boundary ties
                next_poll = now + poll
            await asyncio.sleep(next_poll - now)
            if runtime.state not in ACTIVE_STATES:
                return
            now = time.monotonic()
            if runtime.state == "created" and now - runtime.created_at >= self.config.sessions.connect_timeout_ms / 1000:
                self._fence_runtime(runtime, "CONNECT_TIMEOUT")
                return
            if runtime.state == "streaming" and now - runtime.last_activity >= self.config.sessions.audio_idle_timeout_ms / 1000:
                # Empty requires an accepted audio.end, not an abandoned socket.
                self._fence_runtime(runtime, "AUDIO_IDLE_TIMEOUT")
                return
            if runtime.cancelled:
                self._fence_runtime(runtime, runtime.error_code or "CLIENT_BACKPRESSURE")
                return
            try:
                # Pool acquisition, query and release are all included. Database
                # uncertainty fails closed but must not impersonate revoked auth.
                deadline = asyncio.get_running_loop().time() + 1
                async with asyncio.timeout_at(deadline):
                    while True:
                        login_sid = runtime.ctx.sid if runtime.ctx is not None else None
                        user, login, row = await self._auth_reads.read(
                            RuntimeAuthKey(runtime.user_id, login_sid, runtime.session_id), deadline)
                        current_sid = runtime.ctx.sid if runtime.ctx is not None else None
                        if current_sid == login_sid:
                            break
                        # WS attachment may replace ctx with another login SID.
                        # Read that identity within the same original budget.
                    authorized = bool(
                        runtime.ctx is not None and runtime.ctx.expires_at > utcnow()
                        and user and user.status == "active" and login
                        and login.revoked_at is None and login.refresh_expires_at > utcnow()
                    )
                    disabled = user is not None and user.status != "active"
                    expired = row is not None and content_expired(row, user)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # An unavailable DB cannot establish revocation or disablement,
                # but the currently attached, verified JWT has a known expiry.
                # A failed read must not disguise that already-known expiry.
                current_ctx = runtime.ctx
                code = ("AUTH_INVALID" if current_ctx is not None
                        and current_ctx.expires_at <= utcnow() else "INTERNAL_ERROR")
                log_exception_metadata(exc, phase="watchdog_auth_read", code=code,
                    session_id=runtime.session_id, attempt_no=runtime.attempt_no)
                self._fence_runtime(runtime, code)
                return
            if runtime.state not in ACTIVE_STATES:
                return
            if expired:
                self._fence_runtime(runtime, "CONTENT_EXPIRED")
                return
            if not authorized:
                self._fence_runtime(runtime, "ACCOUNT_DISABLED" if disabled else "AUTH_INVALID")
                return
            if runtime.retry_expires_at is not None and runtime.retry_expires_at <= time.time():
                await self.expire_retry_audio(time.time())
                return

    @staticmethod
    def _close_code(runtime: RuntimeSession) -> int:
        code = runtime.error_code
        if code == "MESSAGE_TOO_LARGE":
            return 1009
        if code in ("AUDIO_IDLE_TIMEOUT", "AUDIO_INTERRUPTED", "CONNECT_TIMEOUT"):
            return 4408
        if code == "AUTH_INVALID":
            return 4401
        if code in ("ACCOUNT_DISABLED", "FORBIDDEN"):
            return 4403
        if code in ("ASR_FAILED", "ASR_TIMEOUT", "INTERNAL_ERROR", "SERVER_SHUTDOWN", "CLIENT_BACKPRESSURE"):
            return 1011
        if code in ("AUDIO_PROTOCOL_ERROR", "AUDIO_LIMIT_EXCEEDED"):
            return 4400
        return 1000

    @staticmethod
    async def _flush_terminal(send_task: asyncio.Task) -> None:
        """Give the sender a bounded chance to deliver the terminal event."""
        if send_task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(send_task), timeout=2.0)
        except asyncio.CancelledError:
            if not send_task.cancelled():
                raise
        except Exception:
            return

    async def _accept_frame(self, runtime: RuntimeSession, frame: bytes) -> None:
        if runtime.cancelled or runtime.cancel_pending or runtime.ended:
            raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
        if len(frame) > 8008:
            raise ProtocolViolation("MESSAGE_TOO_LARGE")
        if len(frame) <= 8:
            raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
        seq, count = struct.unpack_from("<II", frame)
        if count * 2 != len(frame) - 8 or not 1 <= count <= 4000:
            raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
        if runtime.short_frame:
            raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
        if count < 4000:
            runtime.short_frame = True
        if seq != runtime.last_seq + 1:
            raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
        if runtime.samples + count > runtime.max_samples:
            raise ProtocolViolation("AUDIO_LIMIT_EXCEEDED")
        runtime.audio.extend(frame[8:])
        start = runtime.samples
        runtime.samples += count
        runtime.last_seq = seq
        runtime.emit("audio.ack", seq=seq, total_samples=runtime.samples)
        if runtime.asr:
            try:
                await runtime.asr.feed(frame[8:], start)
            except ProviderError as exc:
                await self._fail(runtime, exc.code)
                raise ProtocolViolation(exc.code) from exc
        now = time.monotonic()
        if start == 0 or now - runtime.progress_saved_at >= 1:
            async with self.db.session_factory() as db:
                _, session = await self._locked_session(db, runtime.user_id, runtime.session_id)
                if session and session.state == "streaming" and session.current_attempt_no == runtime.attempt_no:
                    session.received_samples = runtime.samples
                    await stats_service.record_session(db, session, "active")
                    await db.commit()
            runtime.progress_saved_at = now

    async def _end(self, runtime: RuntimeSession, last_seq, total_samples) -> None:
        if self._closing:
            if id(runtime) in self._draining and runtime.ended:
                if (type(last_seq) is int and type(total_samples) is int
                        and last_seq == runtime.end_last_seq and total_samples == runtime.end_total_samples):
                    runtime.emit("audio.end_ack", last_seq=last_seq, total_samples=total_samples,
                                 attempt_no=runtime.attempt_no)
                    return
                raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
            raise SessionError(503, "SERVER_SHUTDOWN", "语音服务正在停止")
        async with self._operation():
            # One owner completes the short acceptance/handoff even if the WS
            # receiver loses its caller. close() joins this operation marker;
            # cutoff/cancel/TTL still fence the runtime independently.
            owner = asyncio.create_task(self._accept_end(runtime, last_seq, total_samples),
                                        name="svi-end-acceptance")
            interrupted = None
            while not owner.done():
                try:
                    await asyncio.shield(owner)
                except asyncio.CancelledError as exc:
                    if not owner.cancelled() and interrupted is None:
                        interrupted = exc
                except Exception:
                    break
            try:
                owner.result()
            except BaseException as exc:
                if interrupted is not None:
                    logging.getLogger(__name__).warning(
                        "End acceptance failed after caller cancellation: %s", type(exc).__name__)
                    raise interrupted from None
                raise
            if interrupted is not None:
                raise interrupted

    async def _accept_end(self, runtime: RuntimeSession, last_seq, total_samples) -> None:
        if runtime.cancel_pending:
            raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
        if type(last_seq) is not int or type(total_samples) is not int:
            raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
        if runtime.ended:
            if last_seq == runtime.end_last_seq and total_samples == runtime.end_total_samples:
                runtime.emit("audio.end_ack", last_seq=last_seq, total_samples=total_samples,
                             attempt_no=runtime.attempt_no)
                return
            raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
        if runtime.cancelled or last_seq != runtime.last_seq or total_samples != runtime.samples:
            raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
        accepted_at = loop_time()
        deadline = accepted_at + self.config.pipeline.stop_to_result_budget_ms / 1000
        commit_confirmed = False
        try:
            async with self.db.session_factory() as db:
                user, session = await self._locked_session(db, runtime.user_id, runtime.session_id)
                if not session or session.state != "streaming" or session.current_attempt_no != runtime.attempt_no:
                    raise ProtocolViolation("AUDIO_PROTOCOL_ERROR")
                if not await self._authorized(db, runtime, user):
                    raise SessionError(401, "AUTH_INVALID", "登录已失效")
                session.state, session.audio_complete = "finalizing", True
                session.received_samples = runtime.samples
                session.end_accepted_at = utcnow()
                root_transaction = db.sync_session.get_transaction()

                def confirm_end(sync_session):
                    nonlocal commit_confirmed
                    if (commit_confirmed or sync_session.in_nested_transaction()
                            or sync_session.get_transaction() is not root_transaction):
                        return
                    commit_confirmed = True
                    # Root confirmation precedes an outer commit-await return
                    # gate. Do not revive PCM already fenced by cutoff/cancel.
                    if (not self._closing and not runtime.cancelled
                            and runtime.state == "streaming"
                            and self.runtimes.get(runtime.session_id) is runtime):
                        runtime.ended, runtime.state = True, "finalizing"
                        runtime.end_last_seq, runtime.end_total_samples = last_seq, total_samples
                        runtime.end_accepted_at, runtime.deadline = accepted_at, deadline
                        runtime.processing_accepted = True

                event.listen(db.sync_session, "after_commit", confirm_end)
                try:
                    await db.commit()
                finally:
                    event.remove(db.sync_session, "after_commit", confirm_end)
            if not self._processing_may_start(runtime):
                self._fence_runtime(runtime, self._close_code_value)
                raise SessionError(503, "SERVER_SHUTDOWN", "语音服务正在停止")
            if (runtime.cancelled or runtime.state != "finalizing"
                    or not runtime.processing_accepted):
                return
            runtime.emit("audio.end_ack", last_seq=last_seq, total_samples=total_samples,
                         attempt_no=runtime.attempt_no)
            self._start_processing(runtime, self._process(runtime))
        except BaseException:
            if commit_confirmed:
                # The owned handoff failed after durable acceptance. Keep the
                # current identity and let managed CAS cleanup settle it.
                self._fence_runtime(runtime, "INTERNAL_ERROR")
            raise

    def _asr_operation_deadline(self, runtime):
        if getattr(runtime.asr_caps, "replay_rate", None):
            # Cloud ASR may still be replaying backlog at stop (V2 §4.4):
            # min(25 s, max(10 s, drain + 5 s)); a retry replays its unfinished
            # audio: drain + 10 s (T12). Local providers keep V1.
            drain = getattr(runtime.asr, "drain_estimate_s", None)
            drain_s = drain() if callable(drain) else 0.0
            if runtime.replay_retry:
                budget = drain_s + CLOUD_RETRY_EXTRA_S
            else:
                budget = min(CLOUD_FINISH_CAP_S, max(CLOUD_FINISH_FLOOR_S, drain_s + 5.0))
        else:
            budget = self.config.asr.finish_timeout_ms / 1000
        return min(runtime.deadline or float("inf"),
                   (runtime.end_accepted_at or loop_time()) + budget)

    def _asr_deadline(self, runtime):
        deadline = self._asr_operation_deadline(runtime)
        if runtime.audio_lease is not None:
            deadline = min(deadline, runtime.audio_lease.deadline)
        elif runtime.retry_expires_at is not None:
            deadline = min(deadline, loop_time() + max(0, runtime.retry_expires_at - time.time()))
        return deadline

    async def _process(self, runtime: RuntimeSession) -> None:
        if runtime.cancelled or runtime.state != "finalizing":
            return
        if runtime.samples == 0:
            await self._empty(runtime)
            return
        started = loop_time()
        try:
            deadline = self._asr_deadline(runtime)
            with call_scope(CallScope(runtime.user_id, runtime.session_id, runtime.attempt_no)):
                async with asyncio.timeout_at(deadline):
                    try:
                        final: AsrFinal = await runtime.asr.finish(runtime.samples, deadline)
                    except ProviderError as exc:
                        self._capture_asr(runtime, exc)
                        raise
                    finally:
                        self._capture_asr(runtime)
            # A result arriving at/after the audio TTL has no commit entitlement.
            if loop_time() >= deadline or self._audio_expired(runtime):
                raise TimeoutError()
            asr_ms = int((loop_time() - started) * 1000)
            if runtime.stable_text and not final.text.startswith(runtime.stable_text):
                raise ProviderError("invalid_output", "ASR final changed committed prefix")
            # Preserve only the original audio TTL; release closes its lease.
            # The ordinary ASR operation deadline is not a raw-commit deadline.
            audio_expires_at = (runtime.audio_lease.expires_at if runtime.audio_lease is not None
                                else runtime.retry_expires_at)
            audio_deadline = runtime.audio_lease.deadline if runtime.audio_lease is not None else None
            await self._release_audio(runtime)
            if final.no_speech or not final.text.strip():
                await self._empty(runtime)
                return
            if len(final.text) > 32_768:
                await self._fail(runtime, "ASR_OUTPUT_LIMIT")
                return
            runtime.refine_terms = refine_hints(runtime.dictionary_snapshot, final.text)
            runtime.dictionary_snapshot.clear()
            async with self.db.session_factory() as db:
                user, session = await self._locked_session(db, runtime.user_id, runtime.session_id)
                if not session or session.current_attempt_no != runtime.attempt_no or session.state != "finalizing":
                    return
                if content_expired(session, user):
                    valid = False
                    failure_code = "CONTENT_EXPIRED"
                elif not await self._authorized(db, runtime, user):
                    valid = False
                    failure_code = "ACCOUNT_DISABLED" if user and user.status != "active" else "AUTH_INVALID"
                else:
                    attempt = await self._attempt(db, runtime)
                    if ((audio_deadline is not None and loop_time() >= audio_deadline)
                            or (audio_expires_at is not None and time.time() >= audio_expires_at)):
                        raise TimeoutError()
                    valid = True
                    session.state, session.retry_cache_status = "refining", "none"
                    session.llm_hints = runtime.refine_terms
                    attempt.raw_text, attempt.asr_ms = final.text, asr_ms
                    self._apply_asr_metrics(attempt, runtime)
                    await db.commit()
            if not valid:
                await self._fail(runtime, failure_code, retain_audio=False)
                return
            runtime.raw_text, runtime.state = final.text, "refining"
            runtime.emit("asr.final", text=final.text, attempt_no=runtime.attempt_no)
            await self._refine(runtime, final.text, asr_ms)
        except TimeoutError:
            await self._fail(runtime, "ASR_TIMEOUT")
        except ProviderError as exc:
            if exc.code == "ledger_unavailable":
                self._fence_runtime(runtime, "INTERNAL_ERROR")
            else:
                await self._fail(runtime, "ASR_TIMEOUT" if exc.code == "timeout" else "ASR_FAILED")
        except asyncio.CancelledError:
            raise
        except SQLAlchemyError as exc:
            log_exception_metadata(exc, phase="process_sqlalchemy", code="INTERNAL_ERROR",
                session_id=runtime.session_id, attempt_no=runtime.attempt_no)
            self._fence_runtime(runtime, "INTERNAL_ERROR")
        except Exception:
            await self._fail(runtime, "ASR_FAILED")

    async def _refine(self, runtime, raw, asr_ms):
        if runtime.cancelled or runtime.state != "refining":
            return
        started = loop_time()
        deadline = runtime.deadline or started
        try:
            with call_scope(CallScope(runtime.user_id, runtime.session_id, runtime.attempt_no)):
                async with asyncio.timeout_at(deadline):
                    refined = await self.refiner.refine(raw, runtime.refine_terms, deadline=deadline)
        except TimeoutError:
            refined = RefineResult(raw, True, "budget_exceeded")
        except asyncio.CancelledError:
            raise
        except Exception:
            refined = RefineResult(raw, True, "llm_unavailable")
        stop_ms = int((loop_time() - (runtime.end_accepted_at or started)) * 1000)
        await self._ready(runtime, raw, refined, asr_ms,
                          int((loop_time() - started) * 1000), stop_ms)

    async def _attempt(self, db, runtime: RuntimeSession) -> SessionAttempt | None:
        return await db.scalar(
            select(SessionAttempt).where(
                SessionAttempt.user_id == runtime.user_id,
                SessionAttempt.session_id == runtime.session_id,
                SessionAttempt.attempt_no == runtime.attempt_no,
            )
        )

    async def _ready(self, runtime, raw, refined, asr_ms, refine_ms, stop_ms):
        payload = await self._store_result(runtime.session_id, runtime.user_id, runtime.attempt_no,
                                          raw, refined, refine_ms, stop_ms, asr_ms=asr_ms)
        if payload is not None:
            runtime.state = "ready"
            await self._release_audio(runtime)
            runtime.emit("result.ready", attempt_no=runtime.attempt_no, result=payload)
            self._retire(runtime)
        else:
            # Cleanup or credential revocation may win the commit race. Ensure
            # the current attempt does not remain active with no running task.
            await self._fail(runtime, runtime.error_code or "AUTH_INVALID", retain_audio=False)

    async def _store_result(
        self,
        session_id: str,
        user_id: str,
        attempt_no: int,
        raw: str,
        refined,
        refine_ms: int,
        stop_ms: int,
        asr_ms: int | None = None,
    ) -> dict | None:
        text = refined.text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        char_count = sum(1 for ch in text if ch.isalnum())
        async with self.db.session_factory() as db:
            user, session = await self._locked_session(db, user_id, session_id)
            runtime = self.runtimes.get(session_id)
            if (session is None or session.state != "refining" or session.current_attempt_no != attempt_no
                or runtime is None or runtime.cancelled):
                return None
            if content_expired(session, user):
                runtime.error_code = "CONTENT_EXPIRED"
                return None
            if not await self._authorized(db, runtime, user):
                runtime.error_code = "ACCOUNT_DISABLED" if user and user.status != "active" else "AUTH_INVALID"
                return None
            attempt = await db.scalar(
                select(SessionAttempt).where(
                    SessionAttempt.user_id == user_id,
                    SessionAttempt.session_id == session_id,
                    SessionAttempt.attempt_no == attempt_no,
                )
            )
            result = SessionResult(
                user_id=user_id,
                session_id=session_id,
                attempt_no=attempt_no,
                raw_text=raw,
                refined_text=text,
                review_required=refined.review_required,
                review_reason=refined.review_reason,
                text_hash=digest,
                dictionary_version=session.dictionary_version if session else 0,
                char_count=char_count,
                stop_to_result_ms=stop_ms,
            )
            db.add(result)
            await db.flush()
            if session:
                session.state = "ready"
                session.current_result_id = result.id
                session.error_code = None
                session.first_terminal_at = session.first_terminal_at or utcnow()
                session.retry_cache_status = "none"
                await stats_service.record_session(
                    db,
                    session,
                    "ready",
                    has_result=True,
                    char_count=char_count,
                    fallback=refined.review_required,
                )
            if attempt:
                attempt.state = "ready"
                attempt.refine_ms = refine_ms
                attempt.stop_to_result_ms = stop_ms
                attempt.ended_at = utcnow()
            await db.commit()
            return {
                "result_id": result.id,
                "attempt_no": result.attempt_no,
                "attempt_kind": attempt.kind if attempt else "initial",
                "raw_text": raw,
                "refined_text": text,
                "review_required": refined.review_required,
                "review_reason": refined.review_reason,
                "text_hash": digest,
                "dictionary_version": result.dictionary_version,
                "stop_to_result_ms": stop_ms,
                "created_at": utc_iso8601(result.created_at),
            }

    async def _empty(self, runtime: RuntimeSession) -> None:
        async with self.db.session_factory() as db:
            _, session = await self._locked_session(db, runtime.user_id, runtime.session_id)
            if not session or session.state not in ACTIVE_STATES or session.current_attempt_no != runtime.attempt_no:
                return
            session.state, session.retry_cache_status = "empty", "none"
            session.asr_hints, session.llm_hints = null(), null()
            session.first_terminal_at = session.first_terminal_at or utcnow()
            session.current_result_id = None
            attempt = await self._attempt(db, runtime)
            if attempt:
                attempt.state, attempt.ended_at, attempt.raw_text = "empty", utcnow(), None
            await stats_service.record_session(db, session, "empty")
            await db.commit()
        runtime.state = "empty"
        await self._stop_processing(runtime)
        await self._release_audio(runtime)
        runtime.emit("session.empty")
        self._retire(runtime)

    async def _persist_cache_status(self, runtime, status):
        async with self.db.session_factory() as db:
            _, session = await self._locked_session(db, runtime.user_id, runtime.session_id)
            if (session and session.current_attempt_no == runtime.attempt_no
                and session.retry_cache_status != "none"):
                session.retry_cache_status = status
                await db.commit()

    async def _drop_cache(self, runtime, status):
        self._drop_cache_local(runtime, status)

    def _drop_cache_local(self, runtime, status):
        self._close_audio_lease(runtime, status)
        # The privacy TTL is independent from DB pool health. Fence and release
        # local/worker work first; metadata update is not on this critical path.
        if runtime.state in ACTIVE_STATES:
            self._fence_runtime(runtime, "ASR_TIMEOUT")
        else:
            runtime.audio.clear()
            runtime.retry_expires_at = None
            runtime.raw_text = None
            runtime.hint_terms.clear()
            runtime.refine_terms.clear()
            runtime.dictionary_snapshot.clear()
            # Terminal processing may still be confirming provider exit; only join it.
            asr, runtime.asr = runtime.asr, None
            if asr:
                self._background(asr.cancel())
        self._background(self._persist_cache_status(runtime, status))
        self._retire(runtime)

    async def expire_retry_audio(self, now_epoch_seconds: float) -> int:
        expired = [r for r in list(self.runtimes.values())
                   if r.retry_expires_at is not None and r.retry_expires_at <= now_epoch_seconds]
        for runtime in expired:
            await self._drop_cache(runtime, "expired")
        return len(expired)

    async def _enforce_retry_audio_cache(self, keep=None):
        await self.expire_retry_audio(time.time())
        cached = [r for r in self.runtimes.values() if r.state == "failed" and r.audio
                  and r.retry_expires_at is not None]
        cached.sort(key=lambda r: (r.retry_expires_at, r.session_id))
        total_bytes = sum(len(r.audio) for r in cached)
        count = len(cached)
        for runtime in cached:
            if count <= self.config.retry.max_cached_sessions and total_bytes <= self.config.retry.audio_budget_bytes:
                break
            size = len(runtime.audio)
            await self._drop_cache(runtime, "evicted")
            total_bytes -= size
            count -= 1

    async def _fail(self, runtime: RuntimeSession, code: str, *, retain_audio=True) -> dict | None:
        # Preserve c91ea16 terminal protection while retaining the new tenant/
        # attempt CAS. A late protocol error may close the socket, never undo a
        # result whose ready transaction already committed.
        result_committed = False
        retained = False
        failed_at = utcnow()
        if (retain_audio and runtime.audio and runtime.ended
                and code in ("ASR_FAILED", "ASR_TIMEOUT")
                and runtime.audio_lease is None):
            # Establish the first local failure cutoff before any pool/row wait.
            # This timestamp is also the first durable failure timestamp below.
            runtime.retry_expires_at = failed_at.timestamp() + self.config.retry.audio_ttl_seconds
            self._arm_audio_lease(runtime, runtime.retry_expires_at)
        async with self.db.session_factory() as db:
            _, session = await self._locked_session(db, runtime.user_id, runtime.session_id)
            if not session or session.current_attempt_no != runtime.attempt_no:
                return None
            result_committed = session.state == "ready" and session.current_result_id is not None
            if not result_committed and session.state not in ACTIVE_STATES:
                return None
            if not result_committed:
                # Expiry may cancel the first failure transaction itself. The
                # later failclosed CAS must retain that original failure/D,
                # not silently turn an expired cache into "never had audio".
                lease = runtime.audio_lease
                if (session.first_asr_failed_at is None and lease is not None
                        and lease.status == "expired"):
                    session.first_asr_failed_at = datetime.fromtimestamp(
                        lease.expires_at - self.config.retry.audio_ttl_seconds, timezone.utc)
                retained = bool(retain_audio and runtime.audio and session.audio_complete
                                and code in ("ASR_FAILED", "ASR_TIMEOUT"))
                if retained:
                    session.first_asr_failed_at = session.first_asr_failed_at or failed_at
                    runtime.retry_expires_at = session.first_asr_failed_at.timestamp() + self.config.retry.audio_ttl_seconds
                    lease = self._arm_audio_lease(runtime, runtime.retry_expires_at)
                    retained = lease.status == "available" and not self._audio_expired(runtime)
                session.retry_cache_status = "available" if retained else (
                    "expired" if session.first_asr_failed_at else "none")
                session.state, session.error_code = "failed", code
                if not retained:
                    session.asr_hints, session.llm_hints = null(), null()
                session.received_samples = runtime.samples
                session.first_terminal_at = session.first_terminal_at or utcnow()
                attempt = await self._attempt(db, runtime)
                if attempt:
                    attempt.state, attempt.error_code, attempt.ended_at = "failed", code, utcnow()
                    self._apply_asr_metrics(attempt, runtime)
                    if not retained:
                        attempt.raw_text = None
                await stats_service.record_session(db, session, "failed")
                await db.commit()
        retained = retained and bool(runtime.audio) and not self._audio_expired(runtime)
        pending, runtime.pending_resume = runtime.pending_resume, None
        if retained and pending is not None and runtime.audio_lease is not None:
            runtime.audio_lease.resume = pending
        runtime.state = "ready" if result_committed else "failed"
        runtime.error_code = code
        await self._stop_processing(runtime)
        if runtime.asr:
            asr, runtime.asr = runtime.asr, None
            await asr.cancel()
        if retained:
            await self._enforce_retry_audio_cache(keep=runtime)
        else:
            await self._release_audio(runtime)
        event = {"type": "error", "session_id": runtime.session_id, "attempt_no": runtime.attempt_no,
            "error": {"code": code, "message": code, "retryable": retained, "details": {}}}
        runtime.emit("error", attempt_no=runtime.attempt_no, error=event["error"])
        self._retire(runtime)
        return event

    async def close(self, *, graceful=False):
        self.begin_close(graceful=graceful)
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_owned())
        # Callers (including the lock watcher) join one owner. Repeated parent
        # cancellation cannot interrupt rollback or cancel a physical job twice.
        await asyncio.shield(self._close_task)

    async def _drain_runtime(self, runtime):
        task = runtime.task
        if task is None:
            self._fence_runtime(runtime, self._close_code_value)
            return
        if id(runtime) in self._draining and not self._close_abort:
            remaining = max(0.0, (runtime.deadline or loop_time()) - loop_time())
            # wait() does not cancel processing or reset its existing deadlines.
            _, pending = await asyncio.wait({task}, timeout=remaining)
            if pending and runtime.state in ACTIVE_STATES:
                self._fence_runtime(runtime, self._close_code_value)
        if not task.done() and runtime.state in ACTIVE_STATES:
            self._fence_runtime(runtime, self._close_code_value)
        await asyncio.gather(task, return_exceptions=True)

    async def _close_owned(self):
        # Calls already inside a commit/provider-open must publish or roll back
        # before the final runtime snapshot. New calls cannot enter _operation.
        if self._operations:
            await asyncio.gather(*tuple(self._operations))
        # Admission handoff is now settled. Include old attempts that left the
        # current map but still own work; terminal cleanup is joined, not cancelled.
        for runtime in tuple(self._processing_tasks.values()):
            self._closing_runtimes[id(runtime)] = runtime
        runtimes = tuple(self._closing_runtimes.values())
        await asyncio.gather(*(self._drain_runtime(runtime) for runtime in runtimes))
        # _fence_runtime schedules real terminal CAS and ASR cancellation work.
        # Joining it is mandatory: cancelling it loses DB state and permits.
        while self.background_tasks:
            await asyncio.gather(*tuple(self.background_tasks), return_exceptions=True)
            # Completed gather may not suspend; allow finished callbacks to retire tasks.
            await asyncio.sleep(0)
        # Includes an old attempt's watchdog still finishing a batched read
        # after a retry replaced the runtime map entry.
        auth_tasks = tuple(self._auth_tasks)
        for task in auth_tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*auth_tasks, return_exceptions=True)
        await self._auth_reads.close()
        # Terminal socket dispatch has its existing bounded send budget. Wait
        # for receivers/partials to release their transactions before DB close.
        if self._websockets:
            await asyncio.gather(*tuple(self._websockets), return_exceptions=True)
        while self.background_tasks:
            await asyncio.gather(*tuple(self.background_tasks), return_exceptions=True)
            # Completed gather may not suspend; allow finished callbacks to retire tasks.
            await asyncio.sleep(0)
        for runtime in runtimes:
            await self._stop_processing(runtime)
            await self._release_audio(runtime)
        self.runtimes.clear()
        self._closing_runtimes.clear()
        if self._background_failed:
            raise RuntimeError("SESSION_SHUTDOWN_PERSISTENCE_INCOMPLETE")
