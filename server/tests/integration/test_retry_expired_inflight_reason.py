"""Controlled API boundary, not a natural600 or physical-model claim.

Only the admission owner's epoch reader is advanced to D. Auth, database,
ASR worker, cleanup clocks and stored timestamps remain untouched.
"""
import asyncio
from contextvars import ContextVar
import uuid

import httpx
import pytest

from app.services import session_runtime as runtime_module
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_retry_cache_boundaries import (
    PhysicalGate, cache_client, failed_upload, read_session,
)


@pytest.mark.parametrize("new_kind,expected_reason", [("asr", "audio_expired"), ("refine", "in_progress")])
def test_expired_inflight_new_key_reason_and_replay(cache_client, monkeypatch, new_kind, expected_reason):
    client, control = cache_client
    app, manager = client.app, client.app.state.sessions
    headers = auth_headers(register_and_login(client))
    marker = ContextVar("only_retry_owner_epoch", default=None)
    original_time, original_retry = runtime_module.time, manager._retry_admitted
    at_deadline_keys = set()
    observed_reads = []

    class AdmissionClock:
        def __getattr__(self, name):
            return getattr(original_time, name)

        def time(self):
            selected = marker.get()
            if selected is not None and selected[0] is asyncio.current_task():
                observed_reads.append(selected[1])
                return selected[1]
            return original_time.time()

    async def admitted(ctx, sid, kind, key, db, admission):
        token = marker.set((asyncio.current_task(), manager.runtimes[sid].retry_expires_at)
                           if key in at_deadline_keys else None)
        try:
            return await original_retry(ctx, sid, kind, key, db, admission)
        finally:
            marker.reset(token)

    monkeypatch.setattr(runtime_module, "time", AdmissionClock())
    monkeypatch.setattr(manager, "_retry_admitted", admitted)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            item = await failed_upload(http, app, headers, control, 4000)
            gate = PhysicalGate()
            control.gates[(item.sid, 2)] = gate
            key = str(uuid.uuid4())
            async def retry(kind, request_key):
                return await http.post(f"/api/v1/sessions/{item.sid}/retry",
                    headers=headers | {"Idempotency-Key": request_key}, json={"retry_kind": kind})
            try:
                accepted = await retry("asr", key)
                assert accepted.status_code == 202 and accepted.json()["current_attempt_no"] == 2
                async with asyncio.timeout(3):
                    await gate.entered.wait()
                before = await read_session(app, item.runtime.user_id, item.sid)
                retry_runtime = manager.runtimes[item.sid]
                assert before["state"] == "finalizing" and before["current_attempt_no"] == 2
                before_d = await retry("asr", str(uuid.uuid4()))
                assert before_d.status_code == 409
                assert before_d.json()["error"]["details"]["reason"] == "in_progress"
                at_deadline_keys.add(key)
                replay = await retry("asr", key)
                assert replay.status_code == 200 and replay.json()["replayed"] is True
                assert replay.json()["current_attempt_no"] == 2
                new_key = str(uuid.uuid4())
                at_deadline_keys.add(new_key)
                denied = await retry(new_kind, new_key)
                assert denied.status_code == 409
                assert denied.json()["error"]["code"] == "RETRY_UNAVAILABLE"
                assert denied.json()["error"]["details"]["reason"] == expected_reason
                after = await read_session(app, item.runtime.user_id, item.sid)
                assert after == before  # rejection must not start attempt3 or publish a result
                calls = [call for call in control.workers[0].calls if call[0][1:] == (item.sid, 2)]
                assert len(calls) == 1
                if new_kind == "asr":
                    assert observed_reads and all(value == item.expiry for value in observed_reads)
                    assert not retry_runtime.audio
            finally:
                gate.release.set()
    client.portal.call(scenario)
