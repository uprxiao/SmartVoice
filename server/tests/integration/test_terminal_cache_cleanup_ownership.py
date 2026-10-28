"""Real PG/ASGI; controlled expiry entry, Mock cleanup. Not natural 600s."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlalchemy import text

from tests.conftest import auth_headers, register_and_login
from tests.integration.test_normal_shutdown_contract import require_owned_database
from tests.integration.test_retry_cleanup_lifecycle_v2 import settle
from tests.integration.test_retry_dictionary_snapshot_ownership import (
    PhysicalGates, disconnect_after_end, open_failed_attempt, ownership_client,
    stored, until, wait_cache,
)


@pytest.mark.parametrize("cleanup_order", ["pending", "completed"])
def test_terminal_cache_expiry_joins_local_provider_cleanup(
    ownership_client, monkeypatch, record_property, cleanup_order,
):
    require_owned_database()
    client = ownership_client
    actor = register_and_login(client, email="terminal-cleanup@example.invalid")
    owner, headers = actor["user"]["id"], auth_headers(actor)

    async def exercise():
        app, manager = client.app, client.app.state.sessions
        assert manager.config.retry.audio_ttl_seconds == 600
        assert manager.config.asr.finish_timeout_ms == 10000
        assert manager.config.pipeline.stop_to_result_budget_ms == 30000
        gates = PhysicalGates(app, monkeypatch)
        observed = {"order": cleanup_order, "controlled_expiry_entry": True,
                    "natural_600_claimed": False, "scope": "real PG/ASGI; Mock cleanup"}
        wires, owned = [], set()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://terminal-cleanup") as http:
                sid, old, wire = await open_failed_attempt(
                    http, app, headers, gates, wires, target=True)
                old_task, audio = old.task, old.audio
                assert old_task is not None
                owned.add(old_task)
                await until(gates.old_cancel_entered.is_set)
                before = await stored(app, owner, sid)
                assert old.state == before["session"]["state"] == "failed"
                assert before["session"]["retry_cache_status"] == "available"
                assert before["session"]["audio_complete"] and len(audio) == 8000
                assert before["session"]["received_samples"] == 4000
                assert old.asr is None and not old_task.done()
                expiry = old.retry_expires_at
                assert expiry == before["session"]["first_asr_failed_at"].timestamp() + 600
                await disconnect_after_end(wire)
                if cleanup_order == "completed":
                    gates.old_cancel_release.set()
                    async with asyncio.timeout(5):
                        await asyncio.shield(old_task)
                    assert gates.old_cancel_returned.is_set()
                else:
                    assert not old_task.done() and not gates.old_cancel_returned.is_set()

                # Invoke the production maintenance entry with its ORIGINAL D;
                # no clock/TTL/DB mutation. This is explicit controlled observation.
                observed["expired_count"] = await manager.expire_retry_audio(expiry)
                expired = await wait_cache(app, owner, sid, "expired")
                assert observed["expired_count"] == 1
                assert not audio and old.retry_expires_at is None and old.asr is None
                assert expired["session"] == {**before["session"], "retry_cache_status": "expired"}
                assert expired["attempts"] == before["attempts"]
                assert expired["contribution"] == before["contribution"]
                observed.update(cache_empty=True, pg_expired=True,
                    interruptions_after_expiry=gates.old_cancel_interruptions,
                    old_cancelled_after_expiry=old_task.cancelled())

                async def close_manager():
                    await manager.close(graceful=True)
                    observed.update(old_done_at_close=old_task.done(),
                        old_cancelled_at_close=old_task.cancelled(),
                        provider_cancel_returned_at_close=gates.old_cancel_returned.is_set(),
                        provider_close_calls_at_close=gates.provider_close_calls)

                close_task = asyncio.create_task(close_manager())
                owned.add(close_task)
                await asyncio.wait({close_task}, timeout=.15)  # Observation, not product budget.
                observed["close_returned_before_gate_release"] = close_task.done()
                gates.old_cancel_release.set()
                async with asyncio.timeout(5):
                    await asyncio.shield(close_task)
                await asyncio.gather(old_task, return_exceptions=True)
                observed["interruptions_final"] = gates.old_cancel_interruptions
                assert observed["interruptions_after_expiry"] == observed["interruptions_final"] == 0
                assert not observed["old_cancelled_after_expiry"] and not observed["old_cancelled_at_close"]
                assert observed["old_done_at_close"] and observed["provider_cancel_returned_at_close"]
                assert observed["provider_close_calls_at_close"] == 0
                if cleanup_order == "pending":
                    assert not observed["close_returned_before_gate_release"]
                assert gates.streams[(sid, 1)].cancelled and old_task.result() is None
                assert await stored(app, owner, sid) == expired
                assert not manager._processing_tasks and not manager.background_tasks
                assert not manager._auth_tasks and not manager._operations
                await until(lambda: app.state.db.engine.pool.checkedout() == 0)
                async with app.state.db.session_factory() as db:
                    for table in ("results", "insertions", "learning_events"):
                        count = await db.scalar(text(f"SELECT count(*) FROM {table} WHERE user_id=:u"), {"u": owner})
                        assert count == 0
                observed["zero_business_side_effects"] = True
        finally:
            gates.old_cancel_release.set()
            gates.retry_finish_release.set()
            if manager._close_task is not None:
                owned.add(manager._close_task)
            observed["cleanup_errors"] = await settle(wires, owned, gates)
            record_property("terminal_cache_cleanup", json.dumps(observed, sort_keys=True))
            assert not observed["cleanup_errors"], observed["cleanup_errors"]

    client.portal.call(exercise)
