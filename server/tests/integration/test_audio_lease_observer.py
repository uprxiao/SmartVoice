"""Controlled observer check; does not score natural600/audio acceptance."""
import asyncio
import time
from types import SimpleNamespace

from app.services import session_runtime as runtime_module
from app.services.session_runtime import RuntimeSession
from tests.integration.audio_lease_observer import observe_fixed_lease, assert_selected_original_lease
from tests.integration.test_capacity_retry_cache_real600 import clock_sample


def test_fixed_lease_origin_survives_later_epoch_remapping(app_client, monkeypatch):
    manager = app_client.app.state.sessions
    rows = []
    observer = SimpleNamespace(rows=rows, record=lambda kind, **fields: rows.append({"kind": kind, **fields}))
    original_select = manager._asr_deadline

    def selected(runtime):
        value = original_select(runtime)
        observer.record("asr_deadline_selected", session_id=runtime.session_id,
                        selected_deadline_loop=value)
        return value

    monkeypatch.setattr(manager, "_asr_deadline", selected)
    observe_fixed_lease(manager, observer, monkeypatch)
    actual_time = runtime_module.time

    async def scenario():
        now = asyncio.get_running_loop().time()
        runtime = RuntimeSession(session_id="observer-only", user_id="observer-only", device_id="observer-only",
                                 attempt_no=2, deadline=now + 30, end_accepted_at=now)
        expiry = time.time() + 1  # short observer fixture, not a changed production TTL
        lease = manager._arm_audio_lease(runtime, expiry)
        runtime.retry_expires_at = expiry
        try:
            # Controlled +100ms epoch adjustment AFTER the origin was captured.
            # Do not change time.time globally or the actual loop timer.
            monkeypatch.setattr(runtime_module, "time", SimpleNamespace(time=lambda: actual_time.time() + .1))
            value = manager._asr_deadline(runtime)
            fresh = clock_sample()
            remapped_upper = fresh["loop_after"] + max(0, expiry - (fresh["epoch"] + .1))
            assert remapped_upper < value
            assert value == lease.deadline
            assert_selected_original_lease(rows[-1], expiry)
        finally:
            manager._close_audio_lease(runtime)
    app_client.portal.call(scenario)
