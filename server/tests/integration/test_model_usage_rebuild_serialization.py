"""Real-PG regressions for the shared model-usage User fence.

Uses process-owned test DB and two independent production recorder/DB instances.
Ledger rows are explicit synthetic accounting inputs, not model usage evidence.
No advisory backend termination, production service, or real model is involved.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import datetime as dt

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import loop_time
from app.core.db import Database
from app.models import ModelCall, ModelUsageDaily
from app.services.inference_context import CallScope, persist_outcome
from app.services.model_call_recorder import DatabaseCallRecorder
from tests.conftest import register_and_login
from tests.integration.test_stats_pg_recovery import stats_client

STAMP = dt.datetime(2026, 10, 16, 16, 0, tzinfo=dt.timezone.utc)
DAY = dt.date(2026, 10, 17)
PROVIDER, MODEL = "fixture-serialization", "fixture-model"
FIELDS = ("calls", "successes", "failures", "unknown_calls", "elapsed_ms",
          "processed_audio_samples", "prompt_tokens_known", "completion_tokens_known",
          "prompt_tokens_unknown_calls", "completion_tokens_unknown_calls")
EXPECTED = (2, 2, 0, 0, 30, 0, 105, 20, 0, 1)
_WRITER_BOUNDARY = ContextVar('serialization_owned_writer_boundary', default=None)


async def fixture_call(recorder, user_id):
    call_id = await recorder.started(CallScope(user_id), PROVIDER, MODEL, "refine")
    # Fixed historical timestamp is fixture data, not a global clock patch or
    # deadline extension. All writes go through a tenant-owned valid ledger row.
    async with recorder.db.session_factory() as db:
        row = await db.get(ModelCall, call_id)
        assert row is not None and row.user_id == user_id and row.status == "running"
        row.started_at = STAMP
        await db.commit()
    return call_id


async def counts(database, user_id):
    async with database.session_factory() as db:
        row = await db.scalar(select(ModelUsageDaily).where(
            ModelUsageDaily.user_id == user_id, ModelUsageDaily.date == DAY,
            ModelUsageDaily.provider == PROVIDER, ModelUsageDaily.model_id == MODEL,
            ModelUsageDaily.purpose == "refine",
        ))
        assert row is not None
        return tuple(getattr(row, name) for name in FIELDS)


async def finish_with_original_budget(recorder, call_id, boundary=None):
    # The production wrapper still caps ledger work at min(1s, remaining budget).
    # The larger test-harness timeout below never changes this business timeout.
    # The production writer now has its own Task. A context marker follows
    # exactly this helper's call into that child, without replacing any SQL,
    # recorder method, result, timeout or Task cancellation behavior.
    token = _WRITER_BOUNDARY.set(boundary)
    try:
        return await persist_outcome(recorder, {
            "call_id": call_id, "status": "succeeded", "error_code": None,
            "elapsed_ms": 20, "prompt_tokens": 5, "completion_tokens": None,
        }, loop_time() + 5)
    finally:
        _WRITER_BOUNDARY.reset(token)


class ReadBoundary:
    """Pause after the real groups SELECT returns; no query/result is replaced."""
    def __init__(self, monkeypatch, *, pause_groups=True, pause_writer=False):
        self.read = asyncio.Event()
        self.release = asyncio.Event()
        self.writer_locked = asyncio.Event()
        self.writer_release = asyncio.Event()
        self.rebuild_task = None
        self.writer_tasks = set()
        self.child_writer_observed = False
        self.rebuild_pid = self.writer_pid = None
        original = AsyncSession.execute
        original_scalar = AsyncSession.scalar

        async def scalar(session, statement, *args, **kwargs):
            task = asyncio.current_task()
            sql = " ".join(str(statement).lower().split())
            marked = _WRITER_BOUNDARY.get() is self
            writer_lock = (task in self.writer_tasks or marked) and "for update" in sql
            rebuild_lock = task is self.rebuild_task and "for update" in sql and "from users" in sql
            if writer_lock:
                if marked and task not in self.writer_tasks:
                    self.child_writer_observed = True
                self.writer_pid = await original_scalar(session, text("SELECT pg_backend_pid()"))
            if rebuild_lock:
                self.rebuild_pid = await original_scalar(session, text("SELECT pg_backend_pid()"))
            result = await original_scalar(session, statement, *args, **kwargs)
            # Match the top-level Call row lock, not the owner lookup subquery.
            if (pause_writer and writer_lock and "from model_calls" in sql
                    and "from users" not in sql and not self.writer_locked.is_set()):
                self.writer_locked.set()
                await self.writer_release.wait()
            return result

        async def execute(session, statement, *args, **kwargs):
            task = asyncio.current_task()
            sql = " ".join(str(statement).lower().split())
            result = await original(session, statement, *args, **kwargs)
            if (pause_groups and task is self.rebuild_task and not self.read.is_set()
                    and "from model_calls" in sql and "group by" in sql):
                self.rebuild_pid = (await original(session, text("SELECT pg_backend_pid()"))).scalar_one()
                self.read.set()
                await self.release.wait()
            return result

        monkeypatch.setattr(AsyncSession, "execute", execute)
        monkeypatch.setattr(AsyncSession, "scalar", scalar)

    async def blocked_or_completed(self, observer_database, waiter, *, reverse=False):
        # Observe real PG blocker identity, not just a sleeping Python future.
        # Stop well inside the unchanged production ledger timeout of 1s.
        async with asyncio.timeout(0.75):
            while True:
                if waiter.done():
                    await waiter  # Preserve failures; completion without blocking is baseline evidence.
                    return False
                waiter_pid = self.rebuild_pid if reverse else self.writer_pid
                blocker_pid = self.writer_pid if reverse else self.rebuild_pid
                if waiter_pid is not None:
                    async with observer_database.session_factory() as db:
                        blockers = await db.scalar(text("SELECT pg_blocking_pids(:pid)"),
                                                   {"pid": waiter_pid})
                    if blocker_pid is not None and blocker_pid in blockers:
                        return True
                await asyncio.sleep(0.005)


async def clean_tasks(boundary, tasks):
    boundary.release.set()
    boundary.writer_release.set()
    for task in tasks:
        if task and not task.done():
            task.cancel()
    # Test anti-hang only. No real service budget/config is modified.
    async with asyncio.timeout(3):
        await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)


@pytest.mark.parametrize("mode", ["finish_existing", "start_then_finish"])
def test_rebuild_real_read_then_other_recorder_same_key_has_no_lost_increment(stats_client, monkeypatch, record_property, mode):
    client = stats_client
    owner = register_and_login(client, "ledger-serialize@example.com")["user"]["id"]

    async def exercise():
        state = client.app.state
        writer_db = Database(state.config)
        other = DatabaseCallRecorder(writer_db)
        boundary = ReadBoundary(monkeypatch)
        tasks = []
        try:
            first = await fixture_call(state.recorder, owner)
            await state.recorder.finished(first, "succeeded", None, 10, 100, 20)
            pending = await fixture_call(other, owner) if mode == "finish_existing" else None
            boundary.rebuild_task = asyncio.create_task(state.recorder.rebuild_usage_before_recovery())
            tasks.append(boundary.rebuild_task)
            await asyncio.wait_for(boundary.read.wait(), timeout=2)

            async def write():
                nonlocal pending
                if pending is None:
                    pending = await fixture_call(other, owner)
                assert await finish_with_original_budget(other, pending, boundary) is True

            writer = asyncio.create_task(write())
            boundary.writer_tasks.add(writer)
            tasks.append(writer)
            blocked = await boundary.blocked_or_completed(state.db, writer)
            if not blocked:
                record_property("writer_committed_projection", str(await counts(state.db, owner)))
            boundary.release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            async with state.db.session_factory() as db:
                rows = (await db.scalars(select(ModelCall).where(
                    ModelCall.user_id == owner, ModelCall.provider == PROVIDER,
                ))).all()
                assert len(rows) == 2 and all(row.status == "succeeded" for row in rows)
                assert all(row.aggregated_at is not None for row in rows)
                second = next(row for row in rows if row.id == pending)
                assert second.completion_tokens is None  # Unknown is not converted to zero.
                record_property("terminal_source_rows", len(rows))
            # Check permanent ledger before the counter assertion so a failed
            # baseline still proves that both real writes committed.
            actual = await counts(state.db, owner)
            record_property("projection_after_rebuild", str(actual))
            assert actual == EXPECTED
            assert blocked, "the writer must wait for the real rebuild User transaction"
            assert boundary.child_writer_observed, "observe the actual independent metadata writer"
            # Re-entry must reproduce the same sum rather than merely conceal
            # a transient mismatch by adding a second delta.
            await state.recorder.rebuild_usage_before_recovery()
            assert await counts(state.db, owner) == EXPECTED
        finally:
            try:
                await clean_tasks(boundary, tasks)
            finally:
                await writer_db.close()

    client.portal.call(exercise)


def test_finished_holds_real_call_lock_then_rebuild_reads_committed_total(stats_client, monkeypatch):
    client = stats_client
    owner = register_and_login(client, "ledger-reverse@example.com")["user"]["id"]

    async def exercise():
        state = client.app.state
        writer_db = Database(state.config)
        other = DatabaseCallRecorder(writer_db)
        boundary = ReadBoundary(monkeypatch, pause_groups=False, pause_writer=True)
        tasks = []
        try:
            first = await fixture_call(state.recorder, owner)
            await state.recorder.finished(first, "succeeded", None, 10, 100, 20)
            pending = await fixture_call(other, owner)
            writer = asyncio.create_task(finish_with_original_budget(other, pending, boundary))
            boundary.writer_tasks.add(writer)
            tasks.append(writer)
            await asyncio.wait_for(boundary.writer_locked.wait(), timeout=2)
            boundary.rebuild_task = asyncio.create_task(state.recorder.rebuild_usage_before_recovery())
            tasks.append(boundary.rebuild_task)
            blocked = await boundary.blocked_or_completed(state.db, boundary.rebuild_task, reverse=True)
            boundary.writer_release.set()
            values = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            assert values[0] is True  # Still met the original <=1s ledger budget.
            assert await counts(state.db, owner) == EXPECTED
            assert blocked, "rebuild must wait before reading groups while finished owns User"
            assert boundary.child_writer_observed
        finally:
            try:
                await clean_tasks(boundary, tasks)
            finally:
                await writer_db.close()

    client.portal.call(exercise)


def test_cancel_real_user_lock_wait_keeps_running_and_legal_retry_counts_once(stats_client, monkeypatch):
    client = stats_client
    owner = register_and_login(client, "ledger-cancel@example.com")["user"]["id"]

    async def exercise():
        state = client.app.state
        writer_db = Database(state.config)
        other = DatabaseCallRecorder(writer_db)
        boundary = ReadBoundary(monkeypatch)
        tasks = []
        try:
            first = await fixture_call(state.recorder, owner)
            await state.recorder.finished(first, "succeeded", None, 10, 100, 20)
            pending = await fixture_call(other, owner)
            boundary.rebuild_task = asyncio.create_task(state.recorder.rebuild_usage_before_recovery())
            tasks.append(boundary.rebuild_task)
            await asyncio.wait_for(boundary.read.wait(), timeout=2)
            writer = asyncio.create_task(finish_with_original_budget(other, pending, boundary))
            boundary.writer_tasks.add(writer)
            tasks.append(writer)
            assert await boundary.blocked_or_completed(state.db, writer)
            assert boundary.child_writer_observed
            writer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(writer, timeout=2)
            assert writer_db.engine.pool.checkedout() == 0
            async with state.db.session_factory() as db:
                row = await db.get(ModelCall, pending)
                assert row.status == "running" and row.aggregated_at is None
                assert row.prompt_tokens is None and row.completion_tokens is None
            assert await counts(state.db, owner) == (1,1,0,0,10,0,100,20,0,0)
            boundary.release.set()
            await asyncio.wait_for(boundary.rebuild_task, timeout=2)
            assert await finish_with_original_budget(other, pending) is True
            assert await finish_with_original_budget(other, pending) is True
            assert await counts(state.db, owner) == EXPECTED
        finally:
            try:
                await clean_tasks(boundary, tasks)
            finally:
                await writer_db.close()

    client.portal.call(exercise)
