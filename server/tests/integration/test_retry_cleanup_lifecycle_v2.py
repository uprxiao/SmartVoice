"""Short real-PG/ASGI lifecycle controls; synthetic PCM/Mock physical work.

No clock/TTL change, direct terminal writes, real model or natural-600 claim.
Rollback controls target the frozen ASR599 v2 lease, not the historical lab's
different lease schema. Old-task close also applies to the pre-lease source.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import copy
import json
import uuid

import httpx
import pytest
from sqlalchemy import text

from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_admission_atomicity import CommitFault, _active, _fingerprint, _wait_ready
from tests.integration.test_normal_shutdown_contract import require_owned_database
from tests.integration.test_retry_dictionary_snapshot_ownership import (
    PhysicalGates, digest, disconnect_after_end, open_failed_attempt,
    ownership_client, stored, until,
)
from tests.test_dictionary import create_term


class RollbackPause:
    """Pause before the real AsyncSession.rollback; no fabricated SQL result."""
    def __init__(self, manager):
        self.actual = manager._admission_session
        self.entered, self.release, self.confirmed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.used = False

    @asynccontextmanager
    async def admission(self):
        async with self.actual() as db:
            if self.used:
                yield db
                return
            self.used = True
            actual_rollback = db.rollback

            async def rollback():
                self.entered.set()
                await self.release.wait()
                await actual_rollback()
                self.confirmed.set()

            db.rollback = rollback
            try:
                yield db
            finally:
                db.rollback = actual_rollback


async def settle(wires, owned, physical):
    physical.old_cancel_release.set()
    physical.retry_finish_release.set()
    errors = []
    for wire in wires:
        if wire.task is not None and not wire.task.done():
            try:
                await disconnect_after_end(wire)
            except BaseException as exc:
                errors.append({"stage": "disconnect", "type": type(exc).__name__})
    tasks = {task for task in owned if task is not None}
    tasks.update(wire.task for wire in wires if wire.task is not None)
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=8)
        if pending:
            errors.append({"stage": "join", "type": "owned tasks pending"})
            for task in pending:
                if not task.cancelling():
                    task.cancel()
            _, pending = await asyncio.wait(pending, timeout=3)
            if pending:
                errors.append({"stage": "cancel_join", "type": "owned tasks pending"})
        await asyncio.gather(*(task for task in tasks if task.done()), return_exceptions=True)
    return errors


@pytest.mark.parametrize("cleanup_order", ["before_rollback", "after_rollback"])
def test_retry_confirmed_rollback_keeps_original_frozen_context(
    ownership_client, monkeypatch, record_property, cleanup_order,
):
    require_owned_database()
    client = ownership_client
    # Explicit scope guard: old pre-lease source must not fail with AttributeError
    # and be mislabeled as reproduction of the new lease rollback mechanism.
    from app.services.session_runtime import RuntimeSession
    if "audio_lease" not in RuntimeSession.__dataclass_fields__:
        pytest.skip("ASR599 v2 lease required; pre-lease source is not this control")
    actor = register_and_login(client, email="rollback-context-v2@example.invalid")
    owner, headers = actor["user"]["id"], auth_headers(actor)
    term = create_term(client, headers, "Kubernetes", ["k8s"])
    assert term.status_code == 201 and term.json()["dictionary_version"] == 1
    dictionary_before = client.get("/api/v1/dictionary/terms", headers=headers).json()

    async def exercise():
        app, manager = client.app, client.app.state.sessions
        assert manager.config.retry.audio_ttl_seconds == 600
        assert manager.config.asr.finish_timeout_ms == 10000
        assert manager.config.pipeline.stop_to_result_budget_ms == 30000
        physical = PhysicalGates(app, monkeypatch)
        observed = {"scope": "real PG/ASGI, synthetic PCM/Mock; not natural 600s",
                    "order": cleanup_order, "issues": []}
        wires, owned = [], set()
        pause = fault = None
        installed = False
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rollback-context") as http:
                sid, old, wire = await open_failed_attempt(http, app, headers, physical, wires, target=True)
                owned.add(old.task)
                await until(physical.old_cancel_entered.is_set)
                assert old.state == "failed" and old.asr is None and not old.task.done()
                before = await stored(app, owner, sid)
                fingerprint = await _fingerprint(app, owner)
                audio, pcm, expiry = old.audio, bytes(old.audio), old.retry_expires_at
                frozen, hints = copy.deepcopy(old.dictionary_snapshot), list(old.hint_terms)
                refine_terms = copy.deepcopy(old.refine_terms)
                lease = old.audio_lease
                assert lease is not None and lease.owner is old and lease.status == "available"
                lease_identity, timer = (lease.expires_at, lease.deadline), lease.timer
                assert len(pcm) == 8000 and len(frozen) == 1
                assert frozen[0]["id"] == term.json()["id"]
                assert expiry == before["session"]["first_asr_failed_at"].timestamp() + 600
                assert lease.expires_at == expiry
                await disconnect_after_end(wire)
                pause = RollbackPause(manager)
                monkeypatch.setattr(manager, "_admission_session", pause.admission)
                key = str(uuid.uuid4())
                fault = CommitFault(app, owner, key, "retry", sid)
                fault.install()
                installed = True
                request = asyncio.create_task(http.post(f"/api/v1/sessions/{sid}/retry",
                    headers={**headers, "Idempotency-Key": key}, json={"retry_kind": "asr"}))
                owned.add(request)
                await until(pause.entered.is_set)
                new = fault.transient_runtime
                assert fault.fired == 1 and fault.sqlstate == "22012"
                assert new is not old and new.audio is audio and not old.audio
                assert new.audio_lease is lease and lease.owner is new
                assert new.dictionary_snapshot == frozen and new.dictionary_snapshot is not old.dictionary_snapshot
                assert not pause.confirmed.is_set() and not new.processing_accepted
                if cleanup_order == "before_rollback":
                    physical.old_cancel_release.set()
                    async with asyncio.timeout(5):
                        await old.task
                    assert physical.old_cancel_returned.is_set() and not pause.confirmed.is_set()
                pause.release.set()
                response = await request
                assert response.status_code == 503 and response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
                assert pause.confirmed.is_set()
                if cleanup_order == "after_rollback":
                    physical.old_cancel_release.set()
                    async with asyncio.timeout(5):
                        await old.task
                assert physical.old_cancel_returned.is_set() and physical.old_cancel_interruptions == 0
                assert manager.runtimes[sid] is old and old.audio is audio and bytes(audio) == pcm
                assert lease.owner is old and old.audio_lease is lease and lease.status == "available"
                assert lease.timer is timer and (lease.expires_at, lease.deadline) == lease_identity
                assert await _fingerprint(app, owner) == fingerprint and await _active(app) == 0
                observed.update(sqlstate=fault.sqlstate, rollback_confirmed=True,
                    original_expiry_preserved=old.retry_expires_at == expiry,
                    frozen_before_sha256=digest(frozen), frozen_after_sha256=digest(old.dictionary_snapshot),
                    hint_count_before=len(hints), hint_count_after=len(old.hint_terms))
                if old.retry_expires_at != expiry:
                    observed["issues"].append("original retry expiry lost")
                if old.dictionary_snapshot != frozen or old.hint_terms != hints or old.refine_terms != refine_terms:
                    observed["issues"].append("confirmed rollback lost original frozen context")
                await until(lambda: app.state.ready and app.state.database_ok, seconds=12)
                detail = await http.get(f"/api/v1/sessions/{sid}", headers=headers)
                assert detail.status_code == 200 and detail.json()["retry_kind"] == "asr"
                physical.retry_finish_release.set()
                retry = await http.post(f"/api/v1/sessions/{sid}/retry",
                    headers={**headers, "Idempotency-Key": key}, json={"retry_kind": "asr"})
                assert retry.status_code == 202 and retry.json()["current_attempt_no"] == 2
                owned.add(manager.runtimes[sid].task)
                final = await _wait_ready(http, headers, sid)
                assert final["result"]["raw_text"] == physical.raw
                assert final["result"]["dictionary_version"] == 1
                assert physical.feeds[(sid, 1)] == physical.feeds[(sid, 2)]
                expected = {key: frozen[0][key] for key in ("id", "term", "norm", "aliases")}
                expected.update(contexts=[], ambiguous_alias_norms=[])
                inputs = [item["confirmed_terms"] for item in physical.refine_inputs]
                assert all(set(item) == {"transcript", "confirmed_terms"} for item in physical.refine_inputs)
                after = await stored(app, owner, sid)
                observed.update(actual_refine_term_counts=[len(value) for value in inputs],
                                stored_llm_hint_count=len(after["session"]["llm_hints"] or []))
                if inputs != [[expected["term"]]] or after["session"]["llm_hints"] != [expected]:
                    observed["issues"].append("subsequent formal refine lost frozen term identity")
                assert [item[0] for item in after["attempts"]] == [1, 2]
                assert after["session"]["first_asr_failed_at"] == before["session"]["first_asr_failed_at"]
                dictionary_after = await http.get("/api/v1/dictionary/terms", headers=headers)
                assert dictionary_after.status_code == 200 and dictionary_after.json() == dictionary_before
                replay = await http.post(f"/api/v1/sessions/{sid}/retry",
                    headers={**headers, "Idempotency-Key": key}, json={"retry_kind": "asr"})
                assert replay.status_code == 200 and replay.json()["replayed"]
                assert replay.json()["result"]["result_id"] == final["result"]["result_id"]
                assert len(physical.opened) == 2 and not audio and await _active(app) == 0
                await until(lambda: app.state.db.engine.pool.checkedout() == 0)
                async with app.state.db.session_factory() as db:
                    assert await db.scalar(text("SELECT 1")) == 1
                observed["connection_reusable"] = True
                assert not observed["issues"], observed["issues"]
        finally:
            if pause:
                pause.release.set()
            try:
                observed["cleanup_errors"] = await settle(wires, owned, physical)
            finally:
                if installed:
                    fault.remove()
                if pause:
                    monkeypatch.setattr(manager, "_admission_session", pause.actual)
                record_property("asr599_rollback_context", json.dumps(observed, sort_keys=True))
            assert not observed["cleanup_errors"], observed["cleanup_errors"]

    client.portal.call(exercise)


@pytest.mark.parametrize("old_cleanup", ["pending", "completed"])
def test_close_joins_replaced_old_attempt_cleanup(
    ownership_client, monkeypatch, record_property, old_cleanup,
):
    require_owned_database()
    client = ownership_client
    actor = register_and_login(client, email="old-close-v2@example.invalid")
    owner, headers = actor["user"]["id"], auth_headers(actor)

    async def exercise():
        app, manager = client.app, client.app.state.sessions
        assert manager.config.retry.audio_ttl_seconds == 600
        assert manager.config.asr.finish_timeout_ms == 10000
        assert manager.config.pipeline.stop_to_result_budget_ms == 30000
        physical = PhysicalGates(app, monkeypatch)
        observed = {"scope": "real PG/ASGI, synthetic PCM/Mock; replaced-task close",
                    "old_cleanup": old_cleanup}
        wires, owned = [], set()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://old-close") as http:
                sid, old, wire = await open_failed_attempt(http, app, headers, physical, wires, target=True)
                old_task = old.task
                owned.add(old_task)
                await until(physical.old_cancel_entered.is_set)
                assert old.asr is None and not old_task.done()
                await disconnect_after_end(wire)
                retry = await http.post(f"/api/v1/sessions/{sid}/retry",
                    headers={**headers, "Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "asr"})
                assert retry.status_code == 202 and retry.json()["current_attempt_no"] == 2
                new = manager.runtimes[sid]
                assert new is not old and new.attempt_no == 2 and not old.audio
                owned.add(new.task)
                await until(physical.retry_finish_entered.is_set)
                physical.retry_finish_release.set()
                final = await _wait_ready(http, headers, sid)
                assert final["state"] == "ready" and final["result"]["raw_text"] == physical.raw
                async with asyncio.timeout(5):
                    await new.task
                assert physical.feeds[(sid, 1)] == physical.feeds[(sid, 2)]
                before = await stored(app, owner, sid)
                if old_cleanup == "completed":
                    physical.old_cancel_release.set()
                    async with asyncio.timeout(5):
                        await old_task
                else:
                    assert not old_task.done() and not physical.old_cancel_returned.is_set()
                assert manager.runtimes.get(sid) is not old

                async def close_manager():
                    await manager.close(graceful=True)
                    observed.update(old_task_done_at_close_return=old_task.done(),
                        old_cancel_confirmed_at_close_return=physical.old_cancel_returned.is_set(),
                        provider_close_calls_at_close_return=physical.provider_close_calls)

                close_task = asyncio.create_task(close_manager())
                owned.add(close_task)
                # Only an observer pause; no product deadline is changed. The
                # decisive oracle is captured at actual close return, not after
                # the test finally releases the old cleanup gate.
                await asyncio.wait({close_task}, timeout=.15)
                observed["close_returned_before_test_gate_release"] = close_task.done()
                physical.old_cancel_release.set()
                async with asyncio.timeout(5):
                    await asyncio.gather(old_task, close_task)
                assert observed["old_task_done_at_close_return"]
                assert observed["old_cancel_confirmed_at_close_return"]
                assert observed["provider_close_calls_at_close_return"] == 0
                assert physical.old_cancel_interruptions == 0
                assert await stored(app, owner, sid) == before and await _active(app) == 0
                assert not old.audio and not new.audio and old.asr is new.asr is None
                assert not manager._operations and not manager.background_tasks and not manager._auth_tasks
                await until(lambda: app.state.db.engine.pool.checkedout() == 0)
                async with app.state.db.session_factory() as db:
                    assert await db.scalar(text("SELECT 1")) == 1
                observed["connection_reusable"] = True
        finally:
            observed["cleanup_errors"] = await settle(wires, owned, physical)
            record_property("asr599_replaced_cleanup_close", json.dumps(observed, sort_keys=True))
            assert not observed["cleanup_errors"], observed["cleanup_errors"]

    client.portal.call(exercise)
