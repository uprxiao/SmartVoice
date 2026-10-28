"""One natural600 experiment: 599 physical dispatch + real PG lock + OS exit.

Real manager/MLX adapter/scheduler/DecodeWorker/PG/API/ASGI WS. Only child
transcribe is synthetic. Known parent references only: pickle/send-thread
copies are UNOBSERVED. No clock/TTL/budget/state/model-call mutation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from types import SimpleNamespace
import uuid

import httpx
from sqlalchemy import text

from tests.conftest import auth_headers, register_and_login
from tests.integration.test_asr_real_stop_second_cancel import alive, child_events, read_ledger, real_process_client, until
from tests.integration.test_retry_cache_owned_worker_real600 import natural_owned600, permanent4000
from tests.integration.test_capacity_retry_cache_boundaries import read_session
from tests.integration.test_capacity_retry_cache_confirmed600 import business_facts
from tests.integration.test_capacity_retry_cache_real600 import clock_sample, source_manifest
from tests.integration.test_normal_shutdown_contract import WsProbe, create_session, require_owned_database, safe_status


def test_asr599_natural_dispatch_locked_metadata_known_pcm_and_owned_exit(
    natural_owned600, monkeypatch, record_property,
):
    client, folder, owner, directory = natural_owned600
    require_owned_database()
    actor = register_and_login(client, email="natural599-locked@example.invalid")
    user_id, headers = actor["user"]["id"], auth_headers(actor)

    async def exercise():
        app, loop = client.app, asyncio.get_running_loop()
        manager, engine = app.state.sessions, app.state.asr.engine
        worker = engine.workers[0]
        assert app.state.config.retry.audio_ttl_seconds == 600
        assert app.state.config.asr.decode_timeout_ms == 3000
        assert app.state.config.asr.finish_timeout_ms == 10000
        assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
        assert engine.scheduler.slots == len(engine.workers) == 1
        trace = {"completed": False, "source_before": source_manifest(), "states": [], "refine_calls": [],
            "scope": {"real_pg_asgi": True, "real_owned_os": True, "physical_asr": "synthetic",
                      "known_parent_pcm_only": True, "pickle_and_send_thread_copies": "unobserved",
                      "real_mlx": False, "desktop": False}, "cleanup": {}}
        buffers, processes, runtimes = {}, {}, []
        locked, unlock = asyncio.Event(), asyncio.Event()
        wire = monitor_task = lock_task = probe_task = runtime = old_process = old_popen = job = None
        sid = expiry = key = None
        stop_monitor = False
        original_open, original_refine = app.state.asr.open, manager.refiner.refine

        async def opened(options):
            stream = await original_open(options)
            buffers[id(stream.buffer)] = stream.buffer
            return stream

        async def refined(*args, **kwargs):
            trace["refine_calls"].append(clock_sample())
            return await original_refine(*args, **kwargs)

        monkeypatch.setattr(app.state.asr, "open", opened)
        monkeypatch.setattr(manager.refiner, "refine", refined)

        def capture():
            process = worker.process
            if process is not None:
                assert process._parent_pid == os.getpid()
                processes.setdefault(process.pid, (process, process._popen))
            for item in runtimes:
                buffers[id(item.audio)] = item.audio
            for value in getattr(engine, "_audio_inputs", {}).values():
                if isinstance(value, bytearray):
                    buffers[id(value)] = value
            transient_lengths = []
            # Only named PCM locals in this owned job's coroutine chain; no
            # general frames/locals export and no retained immutable PCM copy.
            coroutine = job.task.get_coro() if job is not None and job.task is not None else None
            while coroutine is not None:
                frame = getattr(coroutine, "cr_frame", None)
                if frame is not None and frame.f_code.co_filename.endswith("/services/asr_core.py"):
                    values = frame.f_locals
                    for name in ("audio", "actual_audio", "supplied_audio", "args"):
                        value = values.get(name)
                        if name == "args" and isinstance(value, tuple) and value:
                            value = value[0]
                        if isinstance(value, bytearray):
                            buffers[id(value)] = value
                        elif isinstance(value, bytes):
                            transient_lengths.append(len(value))
                    del values, frame
                coroutine = getattr(coroutine, "cr_await", None)
            return [len(value) for value in buffers.values()] + transient_lengths

        async def monitor():
            previous, last_recorded = None, -1.0
            while not stop_monitor:
                before = clock_sample()
                lengths = capture()
                old_alive = alive(old_process, old_popen) if old_process is not None else None
                running = engine.scheduler.running.get(key) is job if job is not None else None
                row = {"before": before, "after": clock_sample(), "lengths": lengths, "old_alive": old_alive,
                       "old_exitcode": old_popen.returncode if old_popen is not None else None,
                       "permit_owned": running, "job_done": job.done.is_set() if job is not None else None,
                       "current_pid": worker.process.pid if worker.process is not None else None}
                state = (tuple(lengths), old_alive, running, row["job_done"], row["current_pid"])
                if state != previous or before["monotonic_before"] - last_recorded >= 30:
                    trace["states"].append(row)
                    previous, last_recorded = state, before["monotonic_before"]
                if old_alive and (not running or row["job_done"]):
                    trace.setdefault("premature_permit_release", row)
                if old_alive and row["current_pid"] not in (None, old_process.pid):
                    trace.setdefault("replacement_before_exit", row)
                if old_alive is False:
                    trace.setdefault("first_old_exit", row)
                if expiry is not None and before["epoch"] >= expiry - 1:
                    if any(lengths):
                        trace["last_positive_known_pcm"] = row
                        if before["epoch"] >= expiry + 1:
                            trace.setdefault("known_pcm_after_D_plus_1", row)
                    else:
                        trace.setdefault("first_known_pcm_zero", row)
                await asyncio.sleep(.001 if expiry is not None and before["epoch"] >= expiry - 3 else .05)

        async def wait_epoch(target):
            while True:
                if monitor_task.done():
                    monitor_task.result()
                    raise AssertionError("owned observer ended early")
                now = clock_sample()
                if now["epoch"] >= target:
                    return now
                await asyncio.sleep(.001 if target - now["epoch"] < 2 else .5)

        async def hold_rows():
            async with app.state.db.session_factory() as db:
                assert await db.scalar(text("SELECT id FROM users WHERE id=:u FOR UPDATE"), {"u": user_id}) == user_id
                assert await db.scalar(text("SELECT id FROM sessions WHERE user_id=:u AND id=:s FOR UPDATE"),
                                       {"u": user_id, "s": sid}) == sid
                trace["lock_acquired"] = clock_sample()
                locked.set()
                await unlock.wait()
                await db.rollback()
            trace["lock_connection_released"] = clock_sample()

        try:
            monitor_task = asyncio.create_task(monitor())
            async with asyncio.timeout(635):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://owned599") as http:
                    response = await create_session(http, headers)
                    assert response.status_code == 201, safe_status(response)
                    sid = response.json()["session_id"]
                    initial = manager.runtimes[sid]
                    runtimes.append(initial)
                    wire = WsProbe(app, headers, sid)
                    await wire.start()
                    await wire.end()
                    wire.drain_task = asyncio.create_task(wire.drain())
                    await wire.drain_task
                    before = await read_session(app, user_id, sid)
                    assert before["state"] == "failed" and before["retry_cache_status"] == "available"
                    assert before["received_samples"] == 4000 and len(initial.audio) == 8000
                    expiry = before["first_asr_failed_at"].timestamp() + 600
                    assert initial.retry_expires_at == expiry
                    trace.update(session_id=sid, expiry_epoch=expiry, initial_anchor=clock_sample(),
                                 initial_lease_deadline=initial.audio_lease.deadline)
                    permanent = await permanent4000(app, user_id, sid)
                    await wait_epoch(expiry - 2)
                    assert await app.state.asr.health(), "warm worker not available before 599; do not reset D"
                    await wait_epoch(expiry - 1)
                    retry_key = str(uuid.uuid4())
                    trace["retry_request_start"] = clock_sample()
                    response = await http.post(f"/api/v1/sessions/{sid}/retry",
                        headers=headers | {"Idempotency-Key": retry_key}, json={"retry_kind": "asr"})
                    assert response.status_code == 202 and response.json()["current_attempt_no"] == 2
                    runtime = manager.runtimes[sid]
                    runtimes.append(runtime)
                    key = (user_id, sid, 2)
                    await until(lambda: key in engine.scheduler.running and worker.process is not None and any(
                        row["pid"] == worker.process.pid and row["event"] == "synthetic_decode_enter"
                        for row in child_events(folder, owner)), .8)
                    old_process, job = worker.process, engine.scheduler.running[key]
                    old_popen = old_process._popen
                    assert any(capture()), "physical dispatch had no observed positive parent PCM"
                    trace["physical_positive"] = clock_sample()
                    assert expiry - 1 <= trace["retry_request_start"]["epoch"] <= trace["physical_positive"]["epoch"] < expiry
                    frame = job.task.get_coro().cr_frame
                    physical_deadline, call_id = frame.f_locals["actual_deadline"], frame.f_locals["call_id"]
                    del frame
                    operation_deadline = min(runtime.end_accepted_at + 10, runtime.deadline)
                    assert runtime.audio_lease.deadline == trace["initial_lease_deadline"]
                    assert job.deadline == min(operation_deadline, runtime.audio_lease.deadline)
                    assert physical_deadline == min(job.deadline, operation_deadline - 1)
                    assert runtime.deadline == runtime.end_accepted_at + 30 and runtime.retry_expires_at == expiry
                    ledger_before = await read_ledger(app, sid)
                    assert any(row["id"] == call_id and row["attempt_no"] == 2 and row["status"] == "running" for row in ledger_before["calls"])
                    trace.update(old_pid=old_process.pid, call_id=call_id, eligibility_deadline=job.deadline,
                                 physical_deadline=physical_deadline, operation_deadline=operation_deadline)
                    lock_task = asyncio.create_task(hold_rows())
                    await until(locked.is_set, .5)
                    assert trace["lock_acquired"]["epoch"] < expiry
                    await wait_epoch(expiry)
                    assert alive(old_process, old_popen) and not job.done.is_set(), "missed owned stop-positive window"

                    async def probe(slot):
                        trace["probe"] = {"clock": clock_sample(), "old_alive": alive(old_process, old_popen),
                                          "old_exitcode": old_popen.returncode, "slot": slot}
                        return slot

                    probe_deadline = loop.time() + 2
                    probe_task = asyncio.create_task(engine.scheduler.submit(("permit-observer", uuid.uuid4().hex),
                        "high", probe, deadline=probe_deadline, wait_deadline=probe_deadline))
                    while True:
                        observed = {"before": clock_sample(), "lengths": capture(), "lock_held": not lock_task.done()}
                        observed["after"] = clock_sample()
                        if not any(observed["lengths"]) or observed["after"]["epoch"] >= expiry + 1:
                            trace["known_zero_while_locked"] = observed
                            break
                        await asyncio.sleep(.001)
                    assert observed["lock_held"] and not any(observed["lengths"])
                    assert observed["after"]["epoch"] <= expiry + 1
                    trace["unlock_requested"] = clock_sample()
                    unlock.set()
                    await lock_task
                    await wait_epoch(expiry + 2)
                    assert probe_task.done() and probe_task.result() == 0
                    trace["natural_observation_end"] = clock_sample()
                    assert trace["natural_observation_end"]["monotonic_before"] - trace["initial_anchor"]["monotonic_after"] >= 600
                    await until(lambda: runtime.task is None or runtime.task.done(), 5)
                    trace["after"] = await read_session(app, user_id, sid)
                    item = SimpleNamespace(sid=sid, runtime=initial)
                    trace["facts"], trace["ledger"] = await business_facts(app, item), await read_ledger(app, sid)
                    trace["permanent_unchanged"] = await permanent4000(app, user_id, sid) == permanent
                    denied = await http.post(f"/api/v1/sessions/{sid}/retry", headers=headers | {"Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "asr"})
                    assert denied.status_code == 409 and denied.json()["error"]["details"]["reason"] == "audio_expired"
                    replay = await http.post(f"/api/v1/sessions/{sid}/retry", headers=headers | {"Idempotency-Key": retry_key}, json={"retry_kind": "asr"})
                    assert replay.status_code == 200 and replay.json()["replayed"] and replay.json()["current_attempt_no"] == 2
                    assert await business_facts(app, item) == trace["facts"]
                    trace["children"] = child_events(folder, owner)
                    assert trace["first_known_pcm_zero"]["after"]["epoch"] <= expiry + 1
                    assert "known_pcm_after_D_plus_1" not in trace
                    assert trace["first_old_exit"]["after"]["epoch"] <= expiry + 2
                    assert trace["probe"]["old_alive"] is False and trace["probe"]["old_exitcode"] == -signal.SIGKILL
                    assert any(row["pid"] == trace["old_pid"] and row["event"] == "sigterm_ignored" for row in trace["children"])
                    assert trace["probe"]["clock"]["loop_after"] <= probe_deadline
                    assert "premature_permit_release" not in trace and "replacement_before_exit" not in trace
                    assert trace["permanent_unchanged"] and trace["refine_calls"] == []
                    assert trace["after"]["state"] == "failed" and trace["after"]["retry_cache_status"] == "expired"
                    assert trace["facts"]["counts"] == {"results": 0, "insertions": 0, "learning_events": 0}
                    calls = trace["ledger"]["calls"]
                    assert len(calls) == 2 and {row["attempt_no"] for row in calls} == {1, 2}
                    assert all(row["purpose"] == "asr_finish" and row["status"] in ("failed", "cancelled", "unknown") and row["ended_at"] and row["aggregated_at"]
                        and row["prompt_tokens"] is None and row["completion_tokens"] is None for row in calls)
                    trace["completed"] = True
        finally:
            trace["evidence_end_before_cleanup"] = clock_sample()
            stop_monitor = True
            unlock.set()
            errors = []
            async def clean(label, awaitable, seconds):
                try:
                    async with asyncio.timeout(seconds):
                        return await awaitable
                except BaseException as exc:
                    errors.append({"stage": label, "type": type(exc).__name__})
            if lock_task is not None:
                await clean("lock", asyncio.shield(lock_task), 3)
                if not lock_task.done():
                    lock_task.cancel()
                    await clean("lock_cancel_join", asyncio.gather(lock_task, return_exceptions=True), 3)
            if monitor_task is not None:
                await clean("monitor", asyncio.shield(monitor_task), 2)
            tasks = [task for task in (probe_task, getattr(wire, "task", None), getattr(wire, "drain_task", None)) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await clean("wire_probe", asyncio.gather(*tasks, return_exceptions=True), 5)
            await clean("manager_close", manager.close(), 6)
            await clean("engine_close", engine.close(), 6)
            emergencies = []
            for pid, (process, popen) in processes.items():
                if alive(process, popen):
                    assert process._parent_pid == os.getpid() and process.pid == pid
                    emergencies.append(pid)
                    process.kill()
                    await clean("owned_emergency_join", asyncio.to_thread(process.join, 2), 3)
            trace["cleanup"] = {"errors": errors, "emergency_owned_pids": emergencies, "all_retained_exited": all(not alive(p, popen) for p, popen in processes.values()),
                                "scheduler": engine.scheduler.snapshot(), "pool_checkedout": app.state.db.engine.pool.checkedout()}
            trace["source_unchanged"] = trace["source_before"] == source_manifest()
            payload = json.dumps(trace, default=str, sort_keys=True, indent=2) + "\n"
            path = directory / "locked-599-summary.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
            record_property("asr599_owned_locked", json.dumps({"path": str(path), "completed": trace["completed"],
                "sha256": hashlib.sha256(payload.encode()).hexdigest(), "scope": trace["scope"]}, sort_keys=True))
        assert trace["completed"] and trace["source_unchanged"] and not trace["cleanup"]["errors"]
        assert not trace["cleanup"]["emergency_owned_pids"]
        assert trace["cleanup"]["all_retained_exited"] and trace["cleanup"]["pool_checkedout"] == 0

    client.portal.call(exercise)
