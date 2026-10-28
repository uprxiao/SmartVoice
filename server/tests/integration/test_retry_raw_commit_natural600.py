"""B/C share one natural600 wait; synthetic decode only, real PG/API/adapter.

B: legal final before D, first raw write blocked until D+.1 must be rejected.
C: raw committed before D, only ordinary Mock refinement crosses D and succeeds.
Neither is the existing after-D final-delivery cohort or real OS/MLX evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid

import httpx
from sqlalchemy import text

from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_retry_cache_boundaries import detail, failed_upload
from tests.integration.test_capacity_retry_cache_confirmed600 import business_facts
from tests.integration.test_capacity_retry_cache_real600 import clock_sample, real600_client, source_manifest
from tests.integration.test_normal_shutdown_contract import require_owned_database


def test_natural600_raw_commit_gate_B_and_durable_raw_refine_C(real600_client, monkeypatch):
    env = real600_client
    client, control, observer = env.client, env.control, env.observer
    require_owned_database()
    headers = {name: auth_headers(register_and_login(client, email=f"raw600-{name}@example.invalid"))
               for name in ("B", "C")}
    trace = {"completed": False, "source_before": source_manifest(), "groups": {}, "controls": {},
             "scope": "real_pg_api_asgi_adapter_scheduler_ledger; synthetic_physical_asr_and_mock_llm; no_os_no_mlx_no_native"}
    raw = "缓存期限验证。"

    async def exercise():
        app, manager = client.app, client.app.state.sessions
        worker, inner = control.workers[0], app.state.llm.inner
        items, runtimes, tasks = {}, {}, []
        released, resume, locked, unlock, lock_entered, refine_entered, refine_release = [asyncio.Event() for _ in range(7)]
        actual_decode, actual_release = worker.decode, manager._release_audio
        actual_lock, actual_generate = manager._locked_session, inner.generate
        lock_task = None

        def name_for(sid):
            return next((name for name, item in items.items() if item.sid == sid), None)

        async def physical_decode(args, deadline):
            owner, job = next(iter(worker.engine.scheduler.running.items()))
            name = name_for(owner[1])
            if name is None or owner[2] != 2 or job.priority != "high":
                return await actual_decode(args, deadline)
            assert len(args[0]) == 16000 and asyncio.get_running_loop().time() < deadline
            worker.active_audio = args[0]
            worker.calls.append((owner, job.priority, len(args[0]), hashlib.sha256(args[0]).hexdigest()))
            control.jobs[(owner[1], owner[2])] = job
            trace["groups"][name]["physical_final"] = clock_sample()
            trace["groups"][name]["physical_deadline_loop"] = deadline
            try:
                return {"words": [(0.0, .5, raw)], "segments": [.5], "seconds": 0.0}
            finally:
                worker.active_audio = None
                args = None

        async def audio_released(runtime):
            await actual_release(runtime)
            name = name_for(runtime.session_id)
            if name is None or runtime.attempt_no != 2:
                return
            if name not in runtimes:
                assert runtime.state == "finalizing", "CONTROL: not the ASR-final release point"
            runtimes[name] = runtime
            trace["groups"][name].setdefault("actual_release_return", clock_sample())
            if name == "B" and not released.is_set():
                released.set()
                await resume.wait()  # Actual release has finished; raw transaction has not begun.

        async def locked_session(db, user_id, sid):
            if name_for(sid) == "B" and resume.is_set() and not lock_entered.is_set():
                trace["controls"]["raw_backend_pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                trace["controls"]["raw_lock_enter"] = clock_sample()
                lock_entered.set()
            return await actual_lock(db, user_id, sid)

        async def generate(request, deadline):
            scope = current_scope.get()
            if scope and name_for(scope.session_id) == "C" and scope.attempt_no == 2 and request.purpose == "refine":
                assert request.call_id and not refine_entered.is_set()
                trace["controls"]["refine_enter"] = clock_sample()
                trace["controls"]["refine_deadline_loop"] = deadline
                trace["controls"]["refine_call_id"] = request.call_id
                refine_entered.set()
                await refine_release.wait()  # Inside the actual scheduled physical call's original deadline.
            return await actual_generate(request, deadline)

        async def wait_epoch(target):
            while clock_sample()["epoch"] < target:
                await asyncio.sleep(.002 if target - clock_sample()["epoch"] < 2 else .5)
            return clock_sample()

        async def facts(item):
            value = await business_facts(app, item)
            async with app.state.db.session_factory() as db:
                args = {"u": item.runtime.user_id, "s": item.sid}
                rows = (await db.execute(text("SELECT attempt_no,raw_text FROM session_attempts "
                    "WHERE user_id=:u AND session_id=:s ORDER BY attempt_no"), args)).all()
                value["raw"] = [{"attempt": n, "present": body is not None, "matches": body == raw} for n, body in rows]
                value["retry_calls"] = [dict(row) for row in (await db.execute(text(
                    "SELECT id,purpose,status,processed_audio_samples FROM model_calls "
                    "WHERE user_id=:u AND session_id=:s AND attempt_no=2 ORDER BY started_at,id"), args)).mappings()]
                value["contribution"] = dict((await db.execute(text("SELECT received_samples,counted_session,outcome,has_result "
                    "FROM session_contributions WHERE user_id=:u AND session_id=:s"), args)).mappings().one())
                value["daily"] = {key: int(value) for key, value in (await db.execute(text(
                    "SELECT sum(sessions) AS sessions,sum(received_audio_samples) AS samples "
                    "FROM usage_daily WHERE user_id=:u"), args)).mappings().one().items()}
            return value

        async def hold_user():
            item = items["B"]
            async with app.state.db.session_factory() as db:
                trace["controls"]["holder_pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                assert await db.scalar(text("SELECT id FROM users WHERE id=:u FOR UPDATE"), {"u": item.runtime.user_id}) == item.runtime.user_id
                trace["controls"]["user_locked"] = clock_sample()
                locked.set()
                try:
                    await unlock.wait()
                finally:
                    await db.rollback()
            trace["controls"]["user_unlocked"] = clock_sample()

        async def confirm_wait():
            async with asyncio.timeout(1):
                await lock_entered.wait()
                async with app.state.db.session_factory() as db:
                    while not await db.scalar(text("WITH RECURSIVE w(pid,depth) AS (SELECT CAST(:p AS integer),0 "
                        "UNION ALL SELECT b,depth+1 FROM w CROSS JOIN LATERAL unnest(pg_blocking_pids(pid)) AS blockers(b) WHERE depth<4) "
                        "SELECT EXISTS(SELECT 1 FROM w WHERE pid=CAST(:h AS integer) AND depth>0)"),
                        {"p": trace["controls"]["raw_backend_pid"], "h": trace["controls"]["holder_pid"]}):
                        await asyncio.sleep(.01)
            trace["controls"]["real_user_wait_confirmed"] = clock_sample()

        monkeypatch.setattr(worker, "decode", physical_decode)
        monkeypatch.setattr(manager, "_release_audio", audio_released)
        monkeypatch.setattr(manager, "_locked_session", locked_session)
        monkeypatch.setattr(inner, "generate", generate)
        try:
            async with asyncio.timeout(620):  # Harness anti-hang, never a product deadline extension.
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
                    for name in ("B", "C"):
                        if name == "C":
                            await asyncio.sleep(4)  # Natural stagger; no open test transaction during this wait.
                        item = items[name] = await failed_upload(http, app, headers[name], control, 8000)
                        observer.add(name, item)
                        trace["groups"][name] = {"expiry": item.expiry, "registered": clock_sample()}
                    trace["failure_spacing_seconds"] = items["C"].expiry - items["B"].expiry
                    assert 4 <= trace["failure_spacing_seconds"] < 8, "CONTROL: failure stagger not achieved"
                    for name in ("B", "C"):
                        item, group = items[name], trace["groups"][name]
                        await wait_epoch(item.expiry - (4 if name == "B" else 2))
                        group["retry_sent"] = clock_sample()
                        response = await http.post(f"/api/v1/sessions/{item.sid}/retry", json={"retry_kind": "asr"},
                            headers=headers[name] | {"Idempotency-Key": str(uuid.uuid4())})
                        assert response.status_code == 202 and response.json()["current_attempt_no"] == 2
                        runtime = manager.runtimes[item.sid]
                        tasks.append(runtime.task)
                        assert runtime.deadline == runtime.end_accepted_at + 30
                        group["total_budget"] = {"started_loop": runtime.end_accepted_at, "deadline_loop": runtime.deadline}
                        if name == "B":
                            async with asyncio.timeout(2):
                                await released.wait()
                                group["before_lock"] = await facts(item)
                                assert group["before_lock"]["raw"][-1] == {"attempt": 2, "present": False, "matches": False}
                                assert any(c["purpose"] == "asr_finish" and c["status"] == "succeeded" for c in group["before_lock"]["retry_calls"])
                                lock_task = asyncio.create_task(hold_user())
                                await locked.wait()
                            assert clock_sample()["epoch"] < item.expiry, "CONTROL: lock established after D"
                            resume.set()
                            await confirm_wait()
                            assert clock_sample()["epoch"] < item.expiry, "CONTROL: raw wait was not before D"
                            trace["controls"]["unlock_requested"] = await wait_epoch(item.expiry + .1)
                            unlock.set()
                            await lock_task
                        else:
                            async with asyncio.timeout(2):
                                await refine_entered.wait()
                                group["durable_before_D"] = await facts(item)
                                group["durable_read_return"] = clock_sample()
                            assert group["durable_read_return"]["epoch"] < item.expiry, "CONTROL: no pre-D durable raw proof"
                            assert group["durable_before_D"]["raw"][-1] == {"attempt": 2, "present": True, "matches": True}
                            assert any(c["id"] == trace["controls"]["refine_call_id"] and c["status"] == "running"
                                       for c in group["durable_before_D"]["retry_calls"])
                            trace["controls"]["refine_release"] = await wait_epoch(item.expiry + .1)
                            assert asyncio.get_running_loop().time() < trace["controls"]["refine_deadline_loop"]
                            refine_release.set()
                        async with asyncio.timeout_at(runtime.deadline + 1):
                            await asyncio.shield(runtime.task)
                        current = await detail(http, headers[name], item.sid)
                        result = current["result"]
                        group["terminal"] = {"state": current["state"], "error_code": current["error_code"],
                            "attempt": current["current_attempt_no"], "has_result": result is not None,
                            "raw_matches": bool(result and result["raw_text"] == raw),
                            "refined_matches": bool(result and result["refined_text"] == raw),
                            "review_required": result["review_required"] if result else None}
                        group["after"] = await facts(item)
                        group["finished"] = clock_sample()
                        assert group["actual_release_return"]["epoch"] < item.expiry
                        assert not item.audio and not runtime.audio and worker.active_audio is None
                        assert all(not stream.buffer for (sid, _), stream in control.streams.items() if sid == item.sid)
                    trace["completed"] = True
        finally:
            resume.set(); unlock.set(); refine_release.set()
            async with asyncio.timeout(35):  # Rescue only; never scored as a successful control.
                if lock_task is not None:
                    await asyncio.gather(lock_task, return_exceptions=True)
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            trace["source_after"] = source_manifest()
            trace["source_unchanged"] = trace["source_before"] == trace["source_after"]
            with (env.directory / "raw-bc-summary.json").open("x", encoding="utf-8") as handle:
                os.chmod(handle.name, 0o600)
                json.dump(trace, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")

    client.portal.call(exercise)
    assert trace["completed"] and trace["source_unchanged"]
    problems = []
    for name, group in trace["groups"].items():
        after, terminal = group["after"], group["terminal"]
        expected = "failed" if name == "B" else "ready"
        if terminal["state"] != expected or terminal["has_result"] != (name == "C"):
            problems.append(f"{name}: incorrect terminal/result")
        if after["raw"][-1]["present"] != (name == "C"):
            problems.append(f"{name}: incorrect durable raw eligibility")
        llm = [c for c in after["retry_calls"] if c["purpose"] in ("refine", "audit")]
        if (name == "B" and llm) or (name == "C" and not any(c["purpose"] == "refine" and c["status"] == "succeeded" for c in llm)):
            problems.append(f"{name}: incorrect physical LLM ledger")
        if name == "C" and (not terminal["raw_matches"] or not terminal["refined_matches"] or terminal["review_required"] is not False):
            problems.append("C: durable raw refinement did not succeed faithfully")
        if terminal["attempt"] != 2 or after["counts"] != {"results": int(name == "C"), "insertions": 0, "learning_events": 0}:
            problems.append(f"{name}: incorrect attempt/side-effect counts")
        if name == "B" and terminal["error_code"] != "ASR_TIMEOUT":
            problems.append("B: expiry did not report ASR_TIMEOUT")
        assert after["daily"] == {"sessions": 1, "samples": 8000}
        assert after["contribution"]["received_samples"] == 8000 and after["contribution"]["counted_session"]
        assert after["term_count"] == 0 and after["dictionary_version"] == 0
    assert not problems, problems
