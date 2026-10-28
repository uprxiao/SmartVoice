"""Natural 600s TTL + valid late ASR callback, separate from natural-three node.

Physical decode is synthetic; original MlxAsrSession creates the actual AsrFinal.
Only delivery of that value is gated and intentionally tolerates cancellation.
No provider deadline/TTL/DB time change, no OS/MLX/native acceptance claim.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
import uuid

import httpx

from app.providers.asr.mlx_whisper import MlxAsrSession
from app.services.inference_context import current_scope
from tests.integration.audio_lease_observer import LEASE_FIELDS, observe_fixed_lease, assert_selected_original_lease
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_normal_completion import AsgiPcmStream
from tests.integration.test_capacity_retry_cache_boundaries import detail, failed_upload
from tests.integration.test_capacity_retry_cache_confirmed600 import business_facts, local_summary
from tests.integration.test_capacity_retry_cache_real600 import (
    clock_sample, permanent_facts, real600_client, source_manifest,
)


def test_hist09_natural600_valid_asr_delivery_after_actual_deadline_is_rejected(real600_client, monkeypatch):
    env = real600_client
    client, observer, control = env.client, env.observer, env.control
    app, manager = client.app, client.app.state.sessions
    observe_fixed_lease(manager, observer, monkeypatch)
    positive_headers = auth_headers(register_and_login(client, email="ttl-valid-positive@example.invalid"))
    late_headers = auth_headers(register_and_login(client, email="ttl-valid-late@example.invalid"))
    trace = {"kind": "real600_valid_late_callback", "source_before": source_manifest(),
             "completed": False, "refine_entries": [], "cancel_count": 0,
             "scope": "PG/API/ASGI + actual adapter; synthetic decode, deliberate delivery gate; no OS/MLX"}
    state = {"positive_sid": None, "late_sid": None, "expiry": None}
    obtained, cancelled, release, delivered = (asyncio.Event() for _ in range(4))
    actual_finish, actual_refine, actual_fail = MlxAsrSession.finish, manager.refiner.refine, manager._fail
    worker = control.workers[0]
    actual_decode = worker.decode
    text_value = "缓存期限验证。"

    async def physical_decode(args, deadline):
        owner, job = next(iter(worker.engine.scheduler.running.items()))
        selected = owner[1] == state["positive_sid"] or owner[1:] == (state["late_sid"], 2)
        if not selected or job.priority != "high":
            return await actual_decode(args, deadline)
        assert args[0] and asyncio.get_running_loop().time() < deadline
        worker.active_audio = args[0]
        worker.calls.append((owner, job.priority, len(args[0]), hashlib.sha256(args[0]).hexdigest()))
        control.jobs[(owner[1], owner[2])] = job
        try:
            return {"words": [(0.0, len(args[0]) / 32000, text_value)],
                    "segments": [len(args[0]) / 32000], "seconds": 0.0}
        finally:
            worker.active_audio = None
            args = None

    async def finish(stream, total_samples, deadline):
        value = await actual_finish(stream, total_samples, deadline)
        if stream.options.session_id != state["late_sid"] or stream.options.attempt_no != 2:
            return value
        # This exact legal shape/text already completed the positive full chain.
        assert value.text == text_value and not value.no_speech
        assert value.processed_samples == total_samples == 8000
        trace["obtained_clock"] = clock_sample()
        trace["original_asr_deadline_loop"] = deadline
        assert trace["obtained_clock"]["epoch"] < state["expiry"]
        obtained.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                trace["cancel_count"] += 1
                trace.setdefault("first_actual_cancellation_clock", clock_sample())
                cancelled.set()
        trace["delivered_clock"] = clock_sample()
        assert trace["delivered_clock"]["epoch"] >= state["expiry"]
        delivered.set()
        return value

    async def refine(*args, **kwargs):
        scope = current_scope.get()
        trace["refine_entries"].append({"session_id": scope.session_id if scope else None, "clock": clock_sample()})
        return await actual_refine(*args, **kwargs)

    async def fail(runtime, *args, **kwargs):
        before = clock_sample()
        try:
            return await actual_fail(runtime, *args, **kwargs)
        finally:
            if runtime.attempt_no == 1:
                trace.setdefault("first_fail_window", {"before": before, "after": clock_sample()})

    monkeypatch.setattr(worker, "decode", physical_decode)
    monkeypatch.setattr(MlxAsrSession, "finish", finish)
    monkeypatch.setattr(manager.refiner, "refine", refine)
    monkeypatch.setattr(manager, "_fail", fail)

    async def scenario():
        wire, retry_task, item = None, None, None
        flushed = 0

        async def checkpoint():
            nonlocal flushed
            stop = len(observer.rows)
            payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in observer.rows[flushed:stop])
            def append():
                with (env.directory / "late-timeline.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(payload)
            await asyncio.to_thread(append)
            flushed = stop

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            try:
                key = str(uuid.uuid4())
                created = await http.post("/api/v1/sessions", headers=positive_headers | {"Idempotency-Key": key},
                    json={"client_request_id": key, "learning_enabled": False})
                assert created.status_code == 201
                state["positive_sid"] = created.json()["session_id"]
                wire = AsgiPcmStream(app, SimpleNamespace(headers=lambda: positive_headers),
                    state["positive_sid"], 73, text_value)
                await wire.start()
                wire.finish_requested.set()
                async with asyncio.timeout(35):
                    await wire.runner
                positive = await detail(http, positive_headers, state["positive_sid"])
                assert positive["state"] == "ready" and positive["result"]["raw_text"] == text_value
                assert positive["result"]["refined_text"] == text_value and positive["result"]["review_required"] is False
                trace["positive"] = {"session_id": state["positive_sid"], "state": "ready",
                                     "result_id": positive["result"]["result_id"], "asr_final_and_ready": True}

                item = await failed_upload(http, app, late_headers, control, 8000)
                state["late_sid"], state["expiry"] = item.sid, item.expiry
                observer.add("late_result", item)
                permanent_before = await permanent_facts(app, item)
                # Start earlier than599 to preserve the production 1s ledger
                # reserve; do not turn off the reserve to fabricate an in-flight
                # 599 physical job. It remains the ORIGINAL first_failure+600.
                retry_at, end_at = item.expiry - 4, item.expiry + 3
                retry_key = str(uuid.uuid4())
                last_checkpoint = time.monotonic()
                async with asyncio.timeout(615):
                    while time.time() < end_at:
                        now = clock_sample()
                        observer.record("late_natural_sample")
                        if retry_task is None and now["epoch"] >= retry_at:
                            assert now["epoch"] < item.expiry and len(item.audio) == 16000
                            trace["retry_start"] = clock_sample()
                            retry_task = asyncio.create_task(http.post(f"/api/v1/sessions/{item.sid}/retry",
                                headers=late_headers | {"Idempotency-Key": retry_key}, json={"retry_kind": "asr"}))
                        if now["epoch"] >= item.expiry and cancelled.is_set() and not release.is_set():
                            # First observe real timeout/cancellation; the test
                            # never invokes cancel(), _fence_runtime or expiry.
                            trace["release_clock"] = clock_sample()
                            release.set()
                        if time.monotonic() - last_checkpoint >= 30 and now["epoch"] < retry_at - 5:
                            await checkpoint()
                            last_checkpoint = time.monotonic()
                        await asyncio.sleep(.005 if now["epoch"] >= retry_at - 1 else .5)
                assert retry_task is not None
                async with asyncio.timeout(5):
                    accepted = await retry_task
                trace["retry_http"] = {"status": accepted.status_code,
                                       "attempt": accepted.json().get("current_attempt_no")}
                assert obtained.is_set() and cancelled.is_set() and delivered.is_set()
                current = await detail(http, late_headers, item.sid)
                trace["current"] = {"state": current["state"], "attempt": current["current_attempt_no"],
                    "result_present": current["result"] is not None, "error_code": current["error_code"],
                    "retry_kind": current["retry_kind"], "reason": current["retry_unavailable_reason"]}
                trace["facts"] = await business_facts(app, item)
                trace["permanent_unchanged"] = await permanent_facts(app, item) == permanent_before
                replay = await http.post(f"/api/v1/sessions/{item.sid}/retry",
                    headers=late_headers | {"Idempotency-Key": retry_key}, json={"retry_kind": "asr"})
                trace["replay"] = {"status": replay.status_code, "replayed": replay.json().get("replayed"),
                    "facts_unchanged": await business_facts(app, item) == trace["facts"]}
                observer.record("late_observation_end")
                trace["local"] = local_summary(observer, item.sid, trace["first_fail_window"])
                trace["deadline_selections"] = [{key: row[key] for key in
                    ("attempt_no", "selected_deadline_loop", "ttl_deadline_lower", "ttl_deadline_upper", *LEASE_FIELDS)}
                    for row in observer.rows if row["kind"] == "asr_deadline_selected" and row.get("session_id") == item.sid]
                trace["completed"] = True
            finally:
                observer.record("late_evidence_end_before_rescue")
                trace["source_after"] = source_manifest()
                trace["source_unchanged"] = trace["source_before"] == trace["source_after"]
                try:
                    async with asyncio.timeout(10):
                        await checkpoint()
                        await asyncio.to_thread((env.directory / "late-summary.json").write_text,
                            json.dumps(trace, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                finally:
                    release.set()  # rescue only, after evidence; never scores success
                    if retry_task is not None and not retry_task.done():
                        retry_task.cancel()
                    if retry_task is not None:
                        async with asyncio.timeout(10):
                            await asyncio.gather(retry_task, return_exceptions=True)
                    if wire is not None:
                        await wire.dispose()

    client.portal.call(scenario)
    assert trace["completed"] and trace["source_unchanged"]
    assert trace["retry_http"] == {"status": 202, "attempt": 2}
    assert trace["current"] == {"state": "failed", "attempt": 2, "result_present": False,
        "error_code": "ASR_TIMEOUT", "retry_kind": "none", "reason": "audio_expired"}
    assert trace["cancel_count"] >= 1
    assert trace["obtained_clock"]["epoch"] < state["expiry"] <= trace["delivered_clock"]["epoch"]
    assert trace["delivered_clock"]["loop_before"] >= trace["original_asr_deadline_loop"]
    selections = trace["deadline_selections"]
    assert selections and all(row["attempt_no"] == 2 for row in selections)
    for row in selections:
        assert_selected_original_lease(row, state["expiry"])
    assert any(row["selected_deadline_loop"] == trace["original_asr_deadline_loop"] for row in selections)
    assert trace["refine_entries"] and all(row["session_id"] == state["positive_sid"] for row in trace["refine_entries"])
    facts = trace["facts"]
    assert facts["counts"] == {"results": 0, "insertions": 0, "learning_events": 0}
    assert facts["dictionary_version"] == 0 and facts["term_count"] == 0
    assert len(facts["attempts"]) == 2 and all(row["state"] == "failed" and not row["has_raw"] for row in facts["attempts"])
    assert all(row["purpose"] in ("asr_decode", "asr_finish") and row["status"] != "running" for row in facts["calls"])
    assert trace["permanent_unchanged"] and trace["replay"] == {"status": 200, "replayed": True, "facts_unchanged": True}
    local = trace["local"]
    assert local["elapsed_monotonic_after_initial_fail_return"] >= 600
    assert local["positive_samples_in_last_second"] > 0
    assert local["first_all_local_zero_upper_clock"]["epoch"] <= state["expiry"] + 1
    assert local["positive_samples_at_or_after_D_plus_1"] == 0
