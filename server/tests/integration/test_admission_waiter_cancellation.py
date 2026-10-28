"""Admission-waiter cancellation regressions; author did not run them.

Real isolated PG/API/auth; Mock ASR only. Not N100 capacity/throughput evidence.
The same file can run on baseline: it never refers to the proposed lock name.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event, func, select
from starlette.requests import Request

from app.api.deps import authenticate
from app.models import OperationRequest, SessionAttempt, VoiceSession
from tests.conftest import auth_headers, register_and_login
from tests.ids import fixture_uuid


@pytest.fixture
def admission_client(monkeypatch, request):
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    return request.getfixturevalue("app_client")


def _accounts(client):
    return [register_and_login(client, email=f"admission-boundary-{n}@example.invalid") for n in range(2)]


async def _counts(app, user_id=None):
    answer = {}
    async with app.state.db.session_factory() as db:
        for model, name in ((VoiceSession, "sessions"), (SessionAttempt, "attempts"),
                            (OperationRequest, "operations")):
            query = select(func.count()).select_from(model)
            if user_id is not None:
                query = query.where(model.user_id == user_id)
            answer[name] = int(await db.scalar(query))
    return answer


@pytest.mark.parametrize("operation", ["create", "retry"])
def test_cancelled_admission_waiter_never_opens_db_session_or_checks_out_connection(admission_client, operation):
    client, tokens = admission_client, _accounts(admission_client)

    async def run():
        app, manager = client.app, client.app.state.sessions
        # Authenticate normally before constructing the queue. This isolates the
        # admission transaction from the deliberately separate HTTP auth queries.
        contexts = [await authenticate(Request({"type": "http", "app": app}), auth_headers(t)["Authorization"])
                    for t in tokens]
        original_lock = manager.capacity.lock
        original_factory = app.state.db.session_factory
        engine = app.state.db.engine.sync_engine
        row_held, release, waiter_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        holder = waiter = None
        paused, sessions_for_waiter, checkouts_for_waiter = False, 0, 0
        keys = [fixture_uuid(f"admission-waiter-{operation}-{n}") for n in range(2)]

        async def gated_capacity_lock(db):
            nonlocal paused
            row = await original_lock(db)
            if asyncio.current_task() is holder and not paused:
                paused = True
                row_held.set()
                await release.wait()
            return row

        def observed_factory(*args, **kwargs):
            nonlocal sessions_for_waiter
            if asyncio.current_task() is waiter:
                sessions_for_waiter += 1
            return original_factory(*args, **kwargs)

        def checkout(dbapi_connection, connection_record, connection_proxy):
            nonlocal checkouts_for_waiter
            if asyncio.current_task() is waiter:
                checkouts_for_waiter += 1

        async def waiting_operation():
            waiter_started.set()
            if operation == "create":
                return await manager.create(contexts[1], keys[1], False)
            # A queued retry must not inspect even a missing resource until it
            # owns admission; cancellation must precede DB/key/attempt creation.
            return await manager.retry(contexts[1], fixture_uuid("admission-missing-session"), "refine", keys[1])

        manager.capacity.lock = gated_capacity_lock
        app.state.db.session_factory = observed_factory
        event.listen(engine, "checkout", checkout)
        try:
            holder = asyncio.create_task(manager.create(contexts[0], keys[0], False))
            async with asyncio.timeout(3):
                await row_held.wait()
                waiter = asyncio.create_task(waiting_operation())
                await waiter_started.wait()
                await asyncio.sleep(0)
                assert not waiter.done()
                # Baseline creates AsyncSession before waiting on the same PG
                # capacity row. The proposed outer Python lock must avoid both.
                assert sessions_for_waiter == 0 and checkouts_for_waiter == 0
                waiter.cancel()
                stopped = await asyncio.gather(waiter, return_exceptions=True)
                assert isinstance(stopped[0], asyncio.CancelledError)
                assert sessions_for_waiter == 0 and checkouts_for_waiter == 0
                assert await _counts(app, contexts[1].user.id) == {"sessions": 0, "attempts": 0, "operations": 0}
                release.set()
                first, created = await holder
                assert created and first.state == "created"
                # Cancelled waiter leaves the next contender able to enter. For
                # create, the identical request ID was not consumed. For retry,
                # the zero operation count above proves no reservation persisted.
                second, created = await manager.create(contexts[1], keys[1], False)
                assert created and second.state == "created"
                assert first.id != second.id
                assert await _counts(app, contexts[1].user.id) == {"sessions": 1, "attempts": 1, "operations": 0}
        finally:
            release.set()
            manager.capacity.lock = original_lock
            app.state.db.session_factory = original_factory
            event.remove(engine, "checkout", checkout)
            for task in (holder, waiter):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (holder, waiter) if task is not None), return_exceptions=True)
            for runtime in list(manager.runtimes.values()):
                if runtime.state == "created":
                    await manager.cancel(runtime.session_id, runtime.user_id)

    client.portal.call(run)
