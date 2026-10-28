"""Coalesce live-runtime authorization reads without caching authorization.

Each waiter owns its original deadline. Only the database read is shared;
authorization/retention decisions remain in the requesting watchdog.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import String, column, select, values

from ..models import LoginSession, User, VoiceSession
from .safe_exception_metadata import log_batch_exception_metadata


@dataclass(frozen=True)
class RuntimeAuthKey:
    user_id: str
    login_sid: str | None
    session_id: str


class RuntimeAuthUnavailable(RuntimeError):
    pass


@dataclass(eq=False)
class _Waiter:
    key: RuntimeAuthKey
    deadline: float
    future: asyncio.Future


class RuntimeAuthBatch:
    def __init__(self, db):
        self._db = db
        self._pending: list[_Waiter] = []
        self._current: tuple[_Waiter, ...] = ()
        self._runner: asyncio.Task | None = None
        self._closed = False

    async def read(self, key: RuntimeAuthKey, deadline: float):
        if self._closed:
            raise RuntimeAuthUnavailable("authorization reader closed")
        loop = asyncio.get_running_loop()
        if deadline <= loop.time():
            raise TimeoutError
        waiter = _Waiter(key, deadline, loop.create_future())
        self._pending.append(waiter)
        self._start()
        try:
            # This includes coalescing, waiting behind a batch, checkout, query,
            # and session release. Cancelling this future never cancels peers.
            async with asyncio.timeout_at(deadline):
                return await waiter.future
        finally:
            if not waiter.future.done():
                waiter.future.cancel()
            if waiter in self._pending:
                self._pending.remove(waiter)
            if self._current and all(w.future.done() for w in self._current):
                runner = self._runner
                if runner is not None and not runner.done() and not runner.cancelling():
                    # No remaining consumer: stop the real query as well.
                    runner.cancel()

    def _start(self):
        if not self._closed and self._runner is None and self._pending:
            self._runner = asyncio.create_task(self._run())

    @staticmethod
    def _fail(waiters):
        for waiter in waiters:
            if not waiter.future.done():
                # Do not forward SQL text/parameters into a runtime error.
                waiter.future.set_exception(RuntimeAuthUnavailable("authorization read unavailable"))

    async def _run(self):
        try:
            while self._pending and not self._closed:
                # One event-loop turn, not a new wall-clock queue allowance.
                await asyncio.sleep(0)
                batch = tuple(w for w in self._pending if not w.future.done())
                self._pending.clear()
                self._current = batch
                if not batch:
                    continue
                try:
                    # Every waiter still expires independently. The shared
                    # query cannot outlive the latest original caller deadline.
                    async with asyncio.timeout_at(max(w.deadline for w in batch)):
                        snapshots = await self._fetch(tuple(dict.fromkeys(w.key for w in batch)))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_batch_exception_metadata(exc, phase="auth_fetch_failed", waiters=batch)
                    self._fail(batch)
                else:
                    now = asyncio.get_running_loop().time()
                    for waiter in batch:
                        if waiter.future.done():
                            continue
                        if now >= waiter.deadline:
                            waiter.future.set_exception(TimeoutError())
                        else:
                            waiter.future.set_result(snapshots[waiter.key])
                self._current = ()
        except Exception as exc:
            # An unexpected reader/mapping error has the same failclosed
            # outcome; never leave the current consumers orphaned.
            log_batch_exception_metadata(exc, phase="auth_batch_failed", waiters=self._current)
            self._fail(self._current)
        finally:
            self._fail(self._current)
            self._current = ()
            self._runner = None
            # Cancellation of the last in-flight consumer must not discard
            # callers that arrived for the next batch while SQL was running.
            self._start()

    async def _fetch(self, keys: tuple[RuntimeAuthKey, ...]):
        checks = values(
            column("user_id", String(36)),
            column("login_sid", String(36)),
            column("session_id", String(36)),
            name="runtime_auth_checks",
        ).data([(key.user_id, key.login_sid, key.session_id) for key in keys])
        query = (select(checks.c.user_id, checks.c.login_sid, checks.c.session_id,
                        User, LoginSession, VoiceSession)
            .select_from(checks)
            .outerjoin(User, User.id == checks.c.user_id)
            .outerjoin(LoginSession, (LoginSession.user_id == checks.c.user_id)
                & (LoginSession.id == checks.c.login_sid))
            .outerjoin(VoiceSession, (VoiceSession.user_id == checks.c.user_id)
                & (VoiceSession.id == checks.c.session_id)))
        async with self._db.session_factory() as db:
            rows = (await db.execute(query)).all()
            snapshots = {RuntimeAuthKey(user_id, sid, session_id): (user, login, session)
                         for user_id, sid, session_id, user, login, session in rows}
            if len(snapshots) != len(keys) or any(key not in snapshots for key in keys):
                raise RuntimeAuthUnavailable("authorization snapshot incomplete")
        # Publish only after the connection/session has been released.
        return snapshots

    async def close(self):
        self._closed = True
        self._fail(self._pending)
        self._pending.clear()
        self._fail(self._current)
        runner = self._runner
        if runner is not None and not runner.done():
            if not runner.cancelling():
                runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
