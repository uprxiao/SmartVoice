"""One natural600 window: idle PCM + legal ASR final delivered after retry D.

Real PG/API/ASGI/Mlx adapter/engine/scheduler/ledger; physical worker synthetic.
The declared fault delays only an actual legal AsrFinal after its call outcome
has persisted. It tolerates cancellation to exercise the production late guard.
No clock/TTL/deadline/state patch; no OS/mic/model-quality or all-C-heap claim.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from types import SimpleNamespace
import uuid

import httpx
from sqlalchemy import text

from app.providers.asr.mlx_whisper import MlxAsrSession
from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_normal_completion import AsgiPcmStream
from tests.integration.test_capacity_retry_cache_boundaries import detail, failed_upload, read_session
from tests.integration.test_capacity_retry_cache_confirmed600 import business_facts, local_summary
from tests.integration.test_capacity_retry_cache_real600 import (
    clock_sample, permanent_facts, real600_client, source_manifest,
)
from tests.test_admin import login_admin


def test_natural600_idle_and_599_valid_late_asr_delivery(real600_client, monkeypatch, record_property):
    env = real600_client
    client, observer, control = env.client, env.observer, env.control
    app, manager = client.app, client.app.state.sessions
    headers = {name: auth_headers(register_and_login(client, email=f"natural-final-{name}@example.invalid"))
               for name in ("positive", "idle", "late")}
    admin = auth_headers(login_admin(client))
    trace = {"completed": False, "source_before": source_manifest(), "cancel_count": 0,
             "fail_windows": {}, "boundary": {}, "refine_entries": [], "cleanup": {},
             "scope": {"real_pg_asgi_adapter_scheduler_ledger": True, "ttl_seconds": 600,
                       "physical_worker": "synthetic", "legal_final_delivery_gate": True,
                       "real_os": False, "real_mlx_quality": False, "all_C_heap": False}}
    state, physical_refs = {"positive_sid": None, "late": None}, {}
    obtained, cancelled, release, delivered = (asyncio.Event() for _ in range(4))
    actual_finish, actual_refine, actual_fail = MlxAsrSession.finish, manager.refiner.refine, manager._fail
    actual_record, worker = observer.record, control.workers[0]
    actual_decode = worker.decode
    text_value = "缓存期限验证。"

    async def physical_decode(args, deadline):
        owner, job = next(iter(worker.engine.scheduler.running.items()))
        late = state["late"]
        selected = owner[1] == state["positive_sid"] or (late is not None and owner[1:] == (late.sid, 2))
        if not selected or job.priority != "high":
            return await actual_decode(args, deadline)
        assert isinstance(args[0], bytearray) and args[0]
        assert asyncio.get_running_loop().time() < deadline
        physical_refs[owner] = args[0]  # Retain mutable object, never make a PCM copy.
        worker.active_audio = args[0]
        worker.calls.append((owner, job.priority, len(args[0]), hashlib.sha256(args[0]).hexdigest()))
        control.jobs[(owner[1], owner[2])] = job
        try:
            if late is not None and owner[1:] == (late.sid, 2):
                assert len(args[0]) == 16000
                current = manager.runtimes[late.sid]
                lease = current.audio_lease
                trace["physical_positive"] = {"clock": clock_sample(), "bytes": len(args[0]),
                    "attempt": owner[2], "deadline_loop": deadline, "job_deadline_loop": job.deadline,
                    "lease_deadline_loop": lease.deadline, "expiry_epoch": lease.expires_at,
                    "same_original_pcm": current.audio is late.audio,
                    "operation_budget_loop": current.end_accepted_at + 10}
                assert lease.expires_at == late.expiry and deadline == job.deadline == lease.deadline
            observer.record("synthetic_physical_positive", session_id=owner[1], attempt_no=owner[2])
            return {"words": [(0.0, len(args[0]) / 32000, text_value)],
                    "segments": [len(args[0]) / 32000], "seconds": 0.0}
        finally:
            worker.active_audio = None
            args = None

    async def finish(stream, total_samples, deadline):
        value = await actual_finish(stream, total_samples, deadline)
        late = state["late"]
        if late is None or (stream.options.session_id, stream.options.attempt_no) != (late.sid, 2):
            return value
        assert value.text == text_value and not value.no_speech and value.processed_samples == total_samples == 8000
        trace["obtained_clock"], trace["original_asr_deadline_loop"] = clock_sample(), deadline
        assert trace["obtained_clock"]["epoch"] < late.expiry
        obtained.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                trace["cancel_count"] += 1
                trace.setdefault("first_actual_cancel", clock_sample())
                cancelled.set()
        trace["delivered_clock"] = clock_sample()
        assert trace["delivered_clock"]["epoch"] >= late.expiry
        delivered.set()
        return value  # Exact actual adapter object; no manufactured success response.

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
                trace["fail_windows"].setdefault(runtime.session_id, {"before": before, "after": clock_sample()})

    def record(kind, **extra):
        row = actual_record(kind, **extra)
        for sid in row["groups"]:
            for owner, audio in physical_refs.items():
                if owner[1] == sid:
                    row["groups"][sid]["lengths"][f"retained_engine_attempt_{owner[2]}"] = len(audio)
        row["observed_after"] = clock_sample()
        return row

    monkeypatch.setattr(worker, "decode", physical_decode)
    monkeypatch.setattr(MlxAsrSession, "finish", finish)
    monkeypatch.setattr(manager.refiner, "refine", refine)
    monkeypatch.setattr(manager, "_fail", fail)
    monkeypatch.setattr(observer, "record", record)

    async def scenario():
        wire, retry_task, items, boundary_tasks = None, None, {}, {}
        flushed = 0

        async def checkpoint():
            nonlocal flushed
            stop = len(observer.rows)
            payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in observer.rows[flushed:stop])
            def append():
                fd = os.open(env.directory / "idle-late-timeline.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write(payload)
            await asyncio.to_thread(append)
            flushed = stop

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            async def boundary(name):
                item = items[name]
                started = clock_sample()
                response = await http.post(f"/api/v1/sessions/{item.sid}/retry",
                    headers=headers[name] | {"Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "asr"})
                body = response.json()
                trace["boundary"][name] = {"request_start": started, "response_end": clock_sample(),
                    "status": response.status_code, "code": body.get("error", {}).get("code"),
                    "reason": body.get("error", {}).get("details", {}).get("reason")}

            try:
                key = str(uuid.uuid4())
                created = await http.post("/api/v1/sessions", headers=headers["positive"] | {"Idempotency-Key": key},
                    json={"client_request_id": key, "learning_enabled": False})
                assert created.status_code == 201
                state["positive_sid"] = created.json()["session_id"]
                wire = AsgiPcmStream(app, SimpleNamespace(headers=lambda: headers["positive"]), state["positive_sid"], 73, text_value)
                await wire.start()
                wire.finish_requested.set()
                async with asyncio.timeout(35):
                    await wire.runner
                positive = await detail(http, headers["positive"], state["positive_sid"])
                assert positive["state"] == "ready" and positive["result"]["raw_text"] == text_value
                assert positive["result"]["refined_text"] == text_value and not positive["result"]["review_required"]
                trace["positive"] = {"actual_final_and_ready": True, "session_id": state["positive_sid"]}
                facts_before = {}
                for name in ("idle", "late"):
                    item = items[name] = await failed_upload(http, app, headers[name], control, 8000)
                    observer.add(name, item)
                    facts_before[name] = await permanent_facts(app, item)
                    assert item.runtime.audio_lease.expires_at == item.expiry
                state["late"] = late = items["late"]
                retry_key, checkpoint_at = str(uuid.uuid4()), time.monotonic()
                end_epoch = max(item.expiry for item in items.values()) + 3
                async with asyncio.timeout(615):
                    while time.time() < end_epoch:
                        now = clock_sample()
                        observer.record("natural_idle_late_sample")
                        if retry_task is None and now["epoch"] >= late.expiry - 1:
                            trace["retry_start"] = clock_sample()
                            assert late.expiry - 1 <= trace["retry_start"]["epoch"] < late.expiry
                            assert len(late.audio) == 16000
                            retry_task = asyncio.create_task(http.post(f"/api/v1/sessions/{late.sid}/retry",
                                headers=headers["late"] | {"Idempotency-Key": retry_key}, json={"retry_kind": "asr"}))
                        if now["epoch"] >= late.expiry and cancelled.is_set() and not release.is_set():
                            trace["release_clock"] = clock_sample()
                            release.set()
                        for name, item in items.items():
                            if now["epoch"] >= item.expiry and name not in boundary_tasks:
                                boundary_tasks[name] = asyncio.create_task(boundary(name))
                        if time.monotonic() - checkpoint_at >= 30 and now["epoch"] < min(i.expiry for i in items.values()) - 5:
                            await checkpoint()
                            checkpoint_at = time.monotonic()
                        await asyncio.sleep(.005 if now["epoch"] >= min(i.expiry for i in items.values()) - 2 else .5)
                assert retry_task is not None and obtained.is_set() and cancelled.is_set() and delivered.is_set()
                async with asyncio.timeout(5):
                    response = await retry_task
                    await asyncio.gather(*boundary_tasks.values())
                trace["retry_http"] = {"status": response.status_code, "attempt": response.json().get("current_attempt_no")}
                trace["groups"] = {}
                for name, item in items.items():
                    current = await detail(http, headers[name], item.sid)
                    row = await read_session(app, item.runtime.user_id, item.sid)
                    facts = await business_facts(app, item)
                    async with app.state.db.session_factory() as db:
                        ledger = [dict(r) for r in (await db.execute(text(
                            "SELECT attempt_no,purpose,status,ended_at IS NOT NULL AS ended,"
                            "aggregated_at IS NOT NULL AS aggregated FROM model_calls "
                            "WHERE user_id=:u AND session_id=:s ORDER BY started_at"),
                            {"u": item.runtime.user_id, "s": item.sid})).mappings()]
                    trace["groups"][name] = {"expiry_epoch": item.expiry,
                        "current": {k: current[k] for k in ("state", "current_attempt_no", "error_code", "retry_kind", "retry_unavailable_reason")},
                        "result_present": current["result"] is not None, "facts": facts, "ledger": ledger,
                        "original_failure_unchanged": row["first_asr_failed_at"] == item.first_failure,
                        "permanent_unchanged": await permanent_facts(app, item) == facts_before[name],
                        "local": local_summary(observer, item.sid, trace["fail_windows"][item.sid])}
                before_replay = await business_facts(app, late)
                replay = await http.post(f"/api/v1/sessions/{late.sid}/retry",
                    headers=headers["late"] | {"Idempotency-Key": retry_key}, json={"retry_kind": "asr"})
                trace["replay"] = {"status": replay.status_code, "replayed": replay.json().get("replayed"),
                                   "facts_unchanged": await business_facts(app, late) == before_replay}
                trace["scheduler"] = app.state.asr.scheduler.snapshot()
                trace["processing_pending"] = sum(not task.done() for task in manager._processing_tasks)
                trace["engine_pcm_bytes"] = sum(map(len, app.state.asr.engine._audio_inputs.values()))
                trace["retained_physical_bytes"] = [len(audio) for audio in physical_refs.values()]
                capacity = await http.get("/api/admin/capacity", headers=admin)
                assert capacity.status_code == 200 and capacity.json()["runtime"] is not None
                trace["capacity"] = {key: capacity.json()["runtime"][key] for key in
                    ("active_sessions", "active_pcm_bytes", "retry_pcm_bytes", "reserved_pcm_bytes")}
                trace["completed"] = True
            finally:
                observer.record("before_rescue")
                trace["source_after"] = source_manifest()
                trace["source_unchanged"] = trace["source_after"] == trace["source_before"]
                release.set()
                pending = [task for task in [retry_task, *boundary_tasks.values()] if task is not None]
                for task in pending:
                    if not task.done():
                        task.cancel()
                try:
                    async with asyncio.timeout(10):
                        await asyncio.gather(*pending, return_exceptions=True)
                        if wire is not None:
                            await wire.dispose()
                    trace["cleanup"]["request_tasks_done"] = all(task.done() for task in pending)
                finally:
                    try:
                        async with asyncio.timeout(10):
                            await checkpoint()
                    finally:
                        path = env.directory / "idle-late-summary.json"
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "w") as handle:
                            json.dump(trace, handle, sort_keys=True, indent=2)
                        record_property("idle_late599", json.dumps({"path": str(path), "completed": trace["completed"],
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "scope": trace["scope"]}, sort_keys=True))

    client.portal.call(scenario)
    assert trace["completed"] and trace["source_unchanged"] and trace["cleanup"]["request_tasks_done"]
    assert trace["retry_http"] == {"status": 202, "attempt": 2}
    assert trace["cancel_count"] >= 1 and trace["physical_positive"]["same_original_pcm"]
    assert trace["obtained_clock"]["epoch"] < state["late"].expiry <= trace["delivered_clock"]["epoch"]
    assert trace["delivered_clock"]["loop_before"] >= trace["original_asr_deadline_loop"]
    assert trace["refine_entries"] and all(r["session_id"] == state["positive_sid"] for r in trace["refine_entries"])
    for name, group in trace["groups"].items():
        attempt, error = (1, "ASR_FAILED") if name == "idle" else (2, "ASR_TIMEOUT")
        assert group["current"] == {"state": "failed", "current_attempt_no": attempt, "error_code": error,
            "retry_kind": "none", "retry_unavailable_reason": "audio_expired"}
        assert not group["result_present"] and group["original_failure_unchanged"] and group["permanent_unchanged"]
        assert group["facts"]["counts"] == {"results": 0, "insertions": 0, "learning_events": 0}
        assert len(group["facts"]["attempts"]) == attempt and all(not r["has_raw"] for r in group["facts"]["attempts"])
        assert group["facts"]["dictionary_version"] == group["facts"]["term_count"] == 0
        assert all(r["purpose"] in ("asr_decode", "asr_finish") and r["status"] != "running" for r in group["facts"]["calls"])
        assert group["ledger"] and all(r["ended"] and r["aggregated"] for r in group["ledger"])
        if name == "late":
            assert any(r["attempt_no"] == 2 and r["purpose"] == "asr_finish" and r["status"] == "succeeded"
                       for r in group["ledger"])
        boundary = trace["boundary"][name]
        assert boundary["request_start"]["epoch"] >= group["expiry_epoch"]
        assert (boundary["status"], boundary["code"], boundary["reason"]) == (409, "RETRY_UNAVAILABLE", "audio_expired")
        local = group["local"]
        assert local["positive_samples_in_last_second"] > 0 and local["elapsed_monotonic_after_initial_fail_return"] >= 600
        assert local["first_all_local_zero_upper_clock"]["epoch"] <= group["expiry_epoch"] + 1
        assert local["positive_samples_at_or_after_D_plus_1"] == 0
    assert trace["replay"] == {"status": 200, "replayed": True, "facts_unchanged": True}
    assert trace["scheduler"] == {"running": 0, "high_waiting": 0, "low_waiting": 0}
    assert trace["processing_pending"] == trace["engine_pcm_bytes"] == 0
    assert not any(trace["retained_physical_bytes"])
    assert trace["capacity"] == {"active_sessions": 0, "active_pcm_bytes": 0, "retry_pcm_bytes": 0, "reserved_pcm_bytes": 0}
