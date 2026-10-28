"""User-confirmed TTL: real 600s, local PCM <= D+1, immediate qualification.

Fresh opt-in node; historical strict-zero-at-D test/evidence stay unchanged.
Production PG/API/ASGI WS/cache/adapter/scheduler; synthetic physical ASR only.
No real OS worker, late successful callback, microphone, MLX quality or native
client claim is made. Those are separate parameters, not implied by this node.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import sys
import time
import uuid

import httpx
from sqlalchemy import text

from app.services import session_runtime as runtime_module
from app.services.inference_context import current_scope
from tests.integration.audio_lease_observer import observe_fixed_lease, assert_selected_original_lease
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_retry_cache_boundaries import (
    PhysicalGate, detail, failed_upload, read_session,
)
from tests.integration.test_capacity_retry_cache_real600 import (
    clock_sample, permanent_facts, real600_client, source_manifest,
)


async def business_facts(app, item):
    """Read permanent facts and no-result side effects, never seed terminal rows."""
    async with app.state.db.session_factory() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        args = {"u": item.runtime.user_id, "s": item.sid}
        counts = {}
        for table in ("results", "insertions", "learning_events"):
            counts[table] = int(await db.scalar(text(
                f"SELECT count(*) FROM {table} WHERE user_id=:u AND session_id=:s"), args))
        attempts = [dict(row) for row in (await db.execute(text(
            "SELECT attempt_no,kind,state,error_code,raw_text IS NOT NULL AS has_raw "
            "FROM session_attempts WHERE user_id=:u AND session_id=:s ORDER BY attempt_no"
        ), args)).mappings()]
        calls = [dict(row) for row in (await db.execute(text(
            "SELECT purpose,status,count(*) AS calls FROM model_calls "
            "WHERE user_id=:u AND session_id=:s GROUP BY purpose,status ORDER BY purpose,status"
        ), args)).mappings()]
        dictionary_version = await db.scalar(text(
            "SELECT version FROM dictionary_state WHERE user_id=:u"), args)
        term_count = int(await db.scalar(text(
            "SELECT count(*) FROM dictionary_terms WHERE user_id=:u"), args))
        return {"counts": counts, "attempts": attempts, "calls": calls,
                "dictionary_version": dictionary_version, "term_count": term_count}


def local_summary(observer, sid, fail_window):
    group = observer.groups[sid]
    rows = [row for row in observer.rows if sid in row["groups"]]
    positive = [row for row in rows if any(row["groups"][sid]["lengths"].values())]
    zero = [row for row in rows if not any(row["groups"][sid]["lengths"].values())]
    first_zero = zero[0] if zero else None
    after_limit = [row for row in positive if row["epoch"] >= group["expiry"] + 1]
    terminal = rows[-1]["observed_after"]
    anchor = group["anchor"]
    drifts = [(row["epoch"] - anchor["epoch"]) -
              (row["monotonic_after"] - anchor["monotonic_after"]) for row in rows]
    return {
        "expiry_epoch": group["expiry"], "ttl_seconds": 600,
        "failure_call_window": fail_window,
        "elapsed_monotonic_after_initial_fail_return":
            terminal["monotonic_before"] - fail_window["after"]["monotonic_after"],
        "last_positive_clock": positive[-1]["observed_after"] if positive else None,
        "first_all_local_zero_upper_clock": first_zero["observed_after"] if first_zero else None,
        "first_all_local_zero_lateness_ms":
            (first_zero["observed_after"]["epoch"] - group["expiry"]) * 1000 if first_zero else None,
        "positive_samples_in_last_second": sum(group["expiry"] - 1 <= row["epoch"] < group["expiry"]
                                                for row in positive),
        "positive_samples_at_or_after_D_plus_1": len(after_limit),
        "clock_drift_range_ms": [min(drifts) * 1000, max(drifts) * 1000],
        "cleanup_rule": "local_pcm_upper_observation_at_or_before_D_plus_1",
        "os_worker_exit": "not_in_this_node",
    }


def test_cap15_confirmed_real600_natural_groups(real600_client, monkeypatch):
    env = real600_client
    client, observer, control = env.client, env.observer, env.control
    manager = client.app.state.sessions
    observe_fixed_lease(manager, observer, monkeypatch)
    # Three registration calls stay below the unchanged 5/IP/hour limit.
    names = ("idle", "retry_at_599", "boundary_request")
    headers = {name: auth_headers(register_and_login(client,
        email=f"confirmed600-{name}@example.invalid")) for name in names}
    source_before = source_manifest()
    outcome = {"kind": "confirmed_ttl_actual600_control", "completed": False,
               "source_before": source_before, "request_rows": {}, "fail_windows": {},
               "refine_calls": [], "server_epoch_reads": [], "scope": {
                   "real_pg_api_asgi_ws": True, "physical_asr": "synthetic",
                   "os_worker_exit": False, "real_mlx": False, "native_client": False}}
    request_scope = ContextVar("confirmed600_retry_observation", default=None)
    actual_fail, actual_retry = manager._fail, manager._retry_admitted
    actual_refine = manager.refiner.refine
    actual_record = observer.record
    actual_time_module = runtime_module.time
    retained_runtime_buffers = {}

    async def observed_fail(runtime, *args, **kwargs):
        before = clock_sample()
        try:
            return await actual_fail(runtime, *args, **kwargs)
        finally:
            if runtime.attempt_no == 1:
                outcome["fail_windows"].setdefault(runtime.session_id, {"before": before, "after": clock_sample()})

    async def observed_retry(ctx, sid, kind, key, db, admission):
        marker = request_scope.set({"session_id": sid, "key": key})
        try:
            return await actual_retry(ctx, sid, kind, key, db, admission)
        finally:
            request_scope.reset(marker)

    class EpochObservation:
        """Return the actual production clock read unchanged; no added await."""
        def __getattr__(self, key):
            return getattr(actual_time_module, key)

        def time(self):
            before = time.monotonic()
            value = actual_time_module.time()
            after = time.monotonic()
            marker = request_scope.get()
            if marker is not None:
                frame = sys._getframe(1)
                outcome["server_epoch_reads"].append({
                    **marker, "epoch": value, "monotonic_before": before, "monotonic_after": after,
                    "function": frame.f_code.co_name, "source_line": frame.f_lineno})
                del frame
            return value

    def retained_record(kind, **extra):
        # Retain mutable runtime buffers even when their owner leaves the map.
        # Do not retain immutable physical PCM bytes and create a test-owned leak.
        for sid in observer.groups:
            runtime = manager.runtimes.get(sid)
            if runtime is not None:
                retained_runtime_buffers.setdefault(sid, {})[id(runtime.audio)] = runtime.audio
        row = actual_record(kind, **extra)
        for sid, buffers in retained_runtime_buffers.items():
            for index, buffer in enumerate(buffers.values()):
                row["groups"][sid]["lengths"][f"retained_runtime_buffer_{index}"] = len(buffer)
        row["observed_after"] = clock_sample()
        return row

    async def observed_refine(*args, **kwargs):
        scope = current_scope.get()
        outcome["refine_calls"].append({"session_id": scope.session_id if scope else None,
                                        "clock": clock_sample()})
        return await actual_refine(*args, **kwargs)

    monkeypatch.setattr(manager, "_fail", observed_fail)
    monkeypatch.setattr(manager, "_retry_admitted", observed_retry)
    monkeypatch.setattr(runtime_module, "time", EpochObservation())
    monkeypatch.setattr(observer, "record", retained_record)
    monkeypatch.setattr(manager.refiner, "refine", observed_refine)

    async def scenario():
        items, initial_facts, tasks, keys = {}, {}, {}, {}
        flushed = 0

        async def checkpoint():
            nonlocal flushed
            stop = len(observer.rows)
            payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in observer.rows[flushed:stop])
            def append():
                with (env.directory / "confirmed-timeline.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(payload)
            await asyncio.to_thread(append)
            flushed = stop

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as http:
            async def post_retry(name, *, first_due_observation):
                item = items[name]
                key = keys[name] = str(uuid.uuid4())
                row = outcome["request_rows"][name] = {
                    "key": key, "first_due_observation": first_due_observation,
                    "request_start": clock_sample(), "expiry_epoch": item.expiry}
                response = await http.post(f"/api/v1/sessions/{item.sid}/retry",
                    headers={**headers[name], "Idempotency-Key": key}, json={"retry_kind": "asr"})
                body = response.json()
                row.update(request_end=clock_sample(), status=response.status_code,
                    attempt=body.get("current_attempt_no"), replayed=body.get("replayed"),
                    code=body.get("error", {}).get("code"),
                    reason=body.get("error", {}).get("details", {}).get("reason"))

            try:
                for name in names:
                    item = await failed_upload(http, client.app, headers[name], control, 8000)
                    items[name] = item
                    observer.add(name, item)
                    initial_facts[name] = await permanent_facts(client.app, item)
                retry_gate = PhysicalGate()
                control.gates[(items["retry_at_599"].sid, 2)] = retry_gate
                due = {"retry_at_599": items["retry_at_599"].expiry - 1,
                       "boundary_request": items["boundary_request"].expiry}
                end_epoch = max(item.expiry for item in items.values()) + 3
                checkpoint_at = time.monotonic()
                previous_clock = None
                # Wall time remains the real configured epoch deadline. The
                # 615s timeout is only anti-hang; it grants no product budget.
                async with asyncio.timeout(615):
                    while True:
                        observed = clock_sample()
                        observer.record("natural_sample")
                        for name, threshold in due.items():
                            if name not in tasks and observed["epoch"] >= threshold:
                                item = items[name]
                                if name != "boundary_request":
                                    assert observed["epoch"] < item.expiry, "retry launch missed the original D"
                                marker = {"first_at_or_after_threshold": observed,
                                          "previous": previous_clock, "threshold_epoch": threshold}
                                if name == "boundary_request":
                                    # Enter the formal API immediately in this
                                    # same loop turn, not after a task scheduling
                                    # delay, final GET, D+2 wait or manual cleanup.
                                    await post_retry(name, first_due_observation=marker)
                                    tasks[name] = None
                                else:
                                    tasks[name] = asyncio.create_task(post_retry(name, first_due_observation=marker))
                        if observed["epoch"] >= end_epoch:
                            break
                        # File I/O occurs only >5s before the earliest boundary.
                        if time.monotonic() - checkpoint_at >= 30 and observed["epoch"] < min(due.values()) - 5:
                            await checkpoint()
                            checkpoint_at = time.monotonic()
                        previous_clock = observed
                        nearest = min(abs(item.expiry - observed["epoch"]) for item in items.values())
                        await asyncio.sleep(.001 if nearest < .05 else .02 if nearest < 5 else .5)
                    async with asyncio.timeout(5):
                        await asyncio.gather(*(task for task in tasks.values() if task is not None))
                observer.record("natural_window_closed")
                outcome["cohorts"] = {}
                for name, item in items.items():
                    body = await detail(http, headers[name], item.sid)
                    row = await read_session(client.app, item.runtime.user_id, item.sid)
                    state = await business_facts(client.app, item)
                    current_facts = await permanent_facts(client.app, item)
                    replay = None
                    if name == "retry_at_599":
                        before = state
                        response = await http.post(f"/api/v1/sessions/{item.sid}/retry",
                            headers={**headers[name], "Idempotency-Key": keys[name]}, json={"retry_kind": "asr"})
                        replay = {"status": response.status_code, "replayed": response.json().get("replayed"),
                                  "attempt": response.json().get("current_attempt_no"),
                                  "facts_unchanged": await business_facts(client.app, item) == before}
                    outcome["cohorts"][name] = {
                        "session_id": item.sid, "state": body["state"], "attempt": body["current_attempt_no"],
                        "retry_kind": body["retry_kind"], "reason": body["retry_unavailable_reason"],
                        "cache_status": row["retry_cache_status"], "has_result": body["result"] is not None,
                        "facts": state, "permanent_unchanged": current_facts == initial_facts[name],
                        "replay": replay, "local": local_summary(observer, item.sid, outcome["fail_windows"][item.sid])}
                outcome["retry_599_physical_started"] = retry_gate.entered.is_set()
                outcome["completed"] = True
            finally:
                # Freeze evidence BEFORE any fixture cleanup or gate rescue.
                observer.record("evidence_end_before_rescue")
                outcome["source_after"] = source_manifest()
                outcome["source_unchanged"] = outcome["source_after"] == source_before
                try:
                    async with asyncio.timeout(10):
                        await checkpoint()
                        await asyncio.to_thread((env.directory / "confirmed-summary.json").write_text,
                            json.dumps(outcome, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                finally:
                    for gate in control.gates.values():
                        gate.release.set()
                    for task in tasks.values():
                        if task is not None and not task.done():
                            task.cancel()
                    async with asyncio.timeout(10):
                        await asyncio.gather(*(task for task in tasks.values() if task is not None), return_exceptions=True)

    client.portal.call(scenario)
    assert outcome["completed"] and outcome["source_unchanged"]
    assert outcome["refine_calls"] == [], "late ASR must not enter even Mock refinement"
    expected = (("idle", 1), ("retry_at_599", 2), ("boundary_request", 1))
    for name, expected_attempt in expected:
        data = outcome["cohorts"][name]
        assert data["state"] == "failed" and data["attempt"] == expected_attempt and not data["has_result"]
        assert data["retry_kind"] == "none" and data["reason"] == "audio_expired" and data["cache_status"] == "expired"
        assert data["permanent_unchanged"]
        assert data["facts"]["counts"] == {"results": 0, "insertions": 0, "learning_events": 0}
        assert data["facts"]["dictionary_version"] == 0 and data["facts"]["term_count"] == 0
        assert len(data["facts"]["attempts"]) == expected_attempt
        assert all(row["state"] == "failed" and not row["has_raw"] for row in data["facts"]["attempts"])
        assert all(row["purpose"] in ("asr_decode", "asr_finish") and row["status"] != "running"
                   for row in data["facts"]["calls"])
        local = data["local"]
        assert local["elapsed_monotonic_after_initial_fail_return"] >= 600
        assert local["positive_samples_in_last_second"] > 0
        assert local["first_all_local_zero_upper_clock"] is not None
        assert local["first_all_local_zero_upper_clock"]["epoch"] <= local["expiry_epoch"] + 1
        assert local["positive_samples_at_or_after_D_plus_1"] == 0
        if expected_attempt == 2:
            assert data["replay"] == {"status": 200, "replayed": True, "attempt": 2, "facts_unchanged": True}
    boundary = outcome["request_rows"]["boundary_request"]
    assert boundary["status"] == 409 and boundary["code"] == "RETRY_UNAVAILABLE" and boundary["reason"] == "audio_expired"
    assert boundary["first_due_observation"]["previous"]["epoch"] < boundary["expiry_epoch"]
    assert boundary["request_start"]["epoch"] >= boundary["expiry_epoch"]
    reads = [row for row in outcome["server_epoch_reads"]
             if row["key"] == boundary["key"] and row["function"] == "_retry_admitted"]
    assert reads and all(row["epoch"] >= boundary["expiry_epoch"] for row in reads)
    for name in ("retry_at_599",):
        retry = outcome["request_rows"][name]
        assert retry["status"] == 202 and retry["attempt"] == 2 and retry["replayed"] is False
        assert retry["request_start"]["epoch"] < retry["expiry_epoch"]
    request599 = outcome["request_rows"]["retry_at_599"]
    assert request599["request_start"]["epoch"] >= request599["expiry_epoch"] - 1
    checks = [row for row in observer.rows if row["kind"] == "asr_deadline_selected"
              and row.get("session_id") == outcome["cohorts"]["retry_at_599"]["session_id"]]
    assert checks and all(row["attempt_no"] == 2 for row in checks)
    for row in checks:
        assert_selected_original_lease(row, request599["expiry_epoch"])
