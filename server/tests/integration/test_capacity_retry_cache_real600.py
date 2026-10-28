"""REAL elapsed 600-second CAP15/HIST09 observation; explicit opt-in.

Run only this explicit node in a frozen isolated source copy. No clock patch,
manual expiry call, shortened TTL, or timing grace. Physical ASR is synthetic;
manager, adapter, scheduler, cleanup loop, PG and API are production paths.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.providers.asr.mlx_whisper import MlxWhisperProvider, MlxAsrSession
from app.services.asr_core import AsrEngine
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_retry_cache_boundaries import (
    PhysicalFaultWorker, PhysicalGate, failed_upload, detail, read_session,
)


def clock_sample():
    # Bound acquisition skew instead of pretending epoch and monotonic are the
    # same clock or using a monotonic origin as an asyncio absolute deadline.
    loop = asyncio.get_running_loop()
    before = time.monotonic()
    loop_before = loop.time()
    epoch = time.time()
    loop_after = loop.time()
    after = time.monotonic()
    return {"epoch": epoch, "monotonic_before": before, "monotonic_after": after,
            "loop_before": loop_before, "loop_after": loop_after,
            "clock_read_span_ms": (after - before) * 1000}


def source_manifest():
    root = Path(__file__).resolve().parents[2]
    sources = [path for folder in ("app", "tests", "migrations")
               for path in (root / folder).rglob("*.py")]
    sources += [root / "config/app.yaml", root / "pyproject.toml", root / "alembic.ini"]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(sources))}


@dataclass
class Observer:
    app: object = None
    control: object = None
    groups: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)

    def add(self, name, item):
        anchor = clock_sample()
        lease = item.runtime.audio_lease
        assert lease is not None and lease.expires_at == item.expiry
        self.groups[item.sid] = {
            "name": name, "item": item, "anchor": anchor,
            "expiry": item.expiry,
            "first_lease": lease,
            "first_lease_deadline_loop": lease.deadline,
            "target_monotonic": anchor["monotonic_after"] + item.expiry - anchor["epoch"],
            # first_failure happened before the observer was attached. Bound its
            # mapped monotonic instant by the bracket around this epoch read;
            # do not demand another 600 seconds after the later add() call.
            "first_failure_monotonic_lower": anchor["monotonic_before"] + item.expiry - 600 - anchor["epoch"],
            "first_failure_monotonic_upper": anchor["monotonic_after"] + item.expiry - 600 - anchor["epoch"],
            "retry_request": None,
        }
        self.record("cohort_registered")

    def record(self, kind, **extra):
        row = {"kind": kind, **clock_sample(), **extra, "groups": {}}
        for sid, group in self.groups.items():
            item = group["item"]
            current = self.app.state.sessions.runtimes.get(sid)
            lengths = {"original_audio": len(item.audio), "initial_runtime": len(item.runtime.audio)}
            if current is not None:
                lengths["current_runtime"] = len(current.audio)
            for (stream_sid, attempt), stream in self.control.streams.items():
                if stream_sid == sid:
                    lengths[f"provider_attempt_{attempt}"] = len(stream.buffer)
            for index, worker in enumerate(self.control.workers):
                if worker.active_audio is not None and worker.calls and worker.calls[-1][0][1] == sid:
                    lengths[f"physical_stub_{index}"] = len(worker.active_audio)
                else:
                    lengths[f"physical_stub_{index}"] = 0
            scheduler = self.app.state.asr.scheduler
            running = sum(owner[1] == sid for owner in scheduler.running)
            waiting = sum(owner[1] == sid for queue in scheduler.queues.values() for owner in queue)
            group_row = {
                "expiry_epoch": group["expiry"], "target_monotonic": group["target_monotonic"],
                "observed_epoch_minus_expiry_ms": (row["epoch"] - group["expiry"]) * 1000,
                "lengths": lengths, "scheduler_running": running, "scheduler_waiting": waiting,
                "current_state": current.state if current is not None else None,
                "runtime_expiry": current.retry_expires_at if current is not None else None,
                "jobs": [
                    {"attempt_no": attempt, "deadline_loop": job.deadline,
                     "remaining_lower_seconds": job.deadline - row["loop_after"],
                     "remaining_upper_seconds": job.deadline - row["loop_before"],
                     "started": job.started, "done": job.done.is_set()}
                    for (job_sid, attempt), job in self.control.jobs.items() if job_sid == sid
                ],
            }
            row["groups"][sid] = group_row
        # A zero observation's upper bound is AFTER reading the references;
        # using the earlier timestamp could falsely place a boundary clear early.
        row["observed_after"] = clock_sample()
        self.rows.append(row)
        return row


class ObservedPhysicalWorker(PhysicalFaultWorker):
    async def decode(self, args, deadline):
        # Parent replaces only physical decode; it clears its own live reference
        # in finally. Original production timeout/cancellation owns that await.
        try:
            return await super().decode(args, deadline)
        finally:
            # This wrapper must release its OWN PCM argument before observing;
            # clearing the parent's local variable cannot clear this reference.
            args = None
            self.control.observer.record("physical_decode_returned")


@pytest.fixture
def real600_client(monkeypatch, request):
    if os.environ.get("SVI_RUN_REAL_600S") != "1":
        pytest.fail("explicit SVI_RUN_REAL_600S=1 is required; this node really waits ten minutes")
    evidence_parent = os.environ.get("SVI_TTL_EVIDENCE_DIR")
    if not evidence_parent:
        pytest.fail("set a private SVI_TTL_EVIDENCE_DIR; no credential or PCM is written")
    directory = Path(evidence_parent) / ("real600-" + uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mlx_whisper")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    observer = Observer()
    control = SimpleNamespace(gates={}, jobs={}, streams={}, workers=[], observer=observer)
    observer.control = control
    original_open, original_cancel = MlxWhisperProvider.open, MlxAsrSession.cancel

    async def physical_prepare(engine):
        engine.model_id = "fixture-real600-no-physical-model"
        engine.workers = [ObservedPhysicalWorker(engine, control)]
        control.workers.extend(engine.workers)

    async def observed_open(provider, options):
        stream = await original_open(provider, options)
        control.streams[(options.session_id, options.attempt_no)] = stream
        observer.record("provider_open", session_id=options.session_id, attempt_no=options.attempt_no)
        return stream

    async def observed_cancel(stream):
        observer.record("provider_cancel_before", session_id=stream.options.session_id,
                        attempt_no=stream.options.attempt_no)
        try:
            return await original_cancel(stream)
        finally:
            observer.record("provider_cancel_after", session_id=stream.options.session_id,
                            attempt_no=stream.options.attempt_no)

    monkeypatch.setattr(AsrEngine, "prepare", physical_prepare)
    monkeypatch.setattr(MlxWhisperProvider, "open", observed_open)
    monkeypatch.setattr(MlxAsrSession, "cancel", observed_cancel)
    client = request.getfixturevalue("app_client")
    observer.app = client.app
    config = client.app.state.config
    assert config.retry.audio_ttl_seconds == 600
    assert config.retry.max_cached_sessions == 20
    assert config.retry.audio_budget_bytes == 134_217_728
    assert config.cleanup.audio_interval_seconds == 1
    assert config.asr.finish_timeout_ms == 10_000 and config.asr.decode_timeout_ms == 3_000
    assert config.pipeline.stop_to_result_budget_ms == 30_000
    assert len(control.workers) == 1 and isinstance(client.app.state.asr, MlxWhisperProvider)
    assert client.app.state.cleanup_task and not client.app.state.cleanup_task.done()
    manager = client.app.state.sessions
    original_expire, original_drop = manager.expire_retry_audio, manager._drop_cache
    original_fence, original_release = manager._fence_runtime, manager._release_audio
    original_deadline = manager._asr_deadline

    async def observed_expire(now_epoch):
        observer.record("production_expiry_scan_before", supplied_epoch=now_epoch)
        try:
            return await original_expire(now_epoch)
        finally:
            observer.record("production_expiry_scan_after", supplied_epoch=now_epoch)

    async def observed_drop(runtime, status):
        observer.record("drop_cache_before", session_id=runtime.session_id, status=status)
        try:
            return await original_drop(runtime, status)
        finally:
            observer.record("drop_cache_after", session_id=runtime.session_id, status=status)

    def observed_fence(runtime, code):
        observer.record("fence_before", session_id=runtime.session_id, code=code)
        try:
            return original_fence(runtime, code)
        finally:
            observer.record("fence_after", session_id=runtime.session_id, code=code)

    async def observed_release(runtime):
        observer.record("release_audio_before", session_id=runtime.session_id)
        try:
            return await original_release(runtime)
        finally:
            observer.record("release_audio_after", session_id=runtime.session_id)

    def observed_deadline(runtime):
        before = clock_sample()
        lease = runtime.audio_lease
        lease_deadline = lease.deadline if lease is not None else None
        deadline = original_deadline(runtime)
        after = clock_sample()
        if runtime.retry_expires_at is not None:
            # Bound the original loop.time()/time.time() reads from outside;
            # no epsilon and no assumption that uvloop and monotonic origins match.
            observer.record("asr_deadline_selected", session_id=runtime.session_id,
                attempt_no=runtime.attempt_no, selected_deadline_loop=deadline,
                ttl_deadline_lower=before["loop_before"] + max(0, runtime.retry_expires_at - after["epoch"]),
                ttl_deadline_upper=after["loop_after"] + max(0, runtime.retry_expires_at - before["epoch"]),
                initial_lease_same=(lease is observer.groups[runtime.session_id]["first_lease"]),
                lease_unchanged_during_call=(runtime.audio_lease is lease and lease.deadline == lease_deadline),
                first_lease_deadline_loop=observer.groups[runtime.session_id]["first_lease_deadline_loop"],
                actual_lease_deadline_loop=lease_deadline,
                selection_before=before, selection_after=after)
        return deadline

    monkeypatch.setattr(manager, "_asr_deadline", observed_deadline)
    monkeypatch.setattr(manager, "expire_retry_audio", observed_expire)
    monkeypatch.setattr(manager, "_drop_cache", observed_drop)
    monkeypatch.setattr(manager, "_fence_runtime", observed_fence)
    monkeypatch.setattr(manager, "_release_audio", observed_release)
    # Every wrapper appends bounded metadata synchronously, then delegates once;
    # no inserted await, DB operation, expiry invocation or clock patch.
    return SimpleNamespace(client=client, control=control, observer=observer, directory=directory)


async def permanent_facts(app, item):
    async with app.state.db.session_factory() as db:
        parameters = {"u": item.runtime.user_id, "s": item.sid}
        contribution = await db.scalar(text(
            "SELECT to_jsonb(t) - 'revision' FROM session_contributions t "
            "WHERE user_id=:u AND session_id=:s"), parameters)
        daily = list((await db.scalars(text(
            "SELECT to_jsonb(t) FROM usage_daily t WHERE user_id=:u ORDER BY date"
        ), parameters)).all())
        metadata = (await db.execute(text(
            "SELECT id,user_id,device_id,created_login_sid,created_at,first_terminal_at,"
            "first_asr_failed_at,received_samples,audio_complete,dictionary_version "
            "FROM sessions WHERE user_id=:u AND id=:s"), parameters)).mappings().one()
        assert contribution and daily
        assert contribution["counted_session"] and contribution["outcome"] == "failed"
        assert contribution["received_samples"] == 8000
        encoded = json.dumps({"contribution": contribution, "daily": daily,
                              "metadata": dict(metadata)}, sort_keys=True, default=str).encode()
        return {"sha256": hashlib.sha256(encoded).hexdigest(), "daily_rows": len(daily),
                "counted_session": contribution["counted_session"],
                "received_samples": contribution["received_samples"]}


def summarize(observer):
    summary = {}
    for sid, group in observer.groups.items():
        rows = [(row, row["groups"][sid]) for row in observer.rows if sid in row["groups"]]
        positive = [(row, state) for row, state in rows if any(state["lengths"].values())]
        late_positive = [(row, state) for row, state in positive if row["epoch"] >= group["expiry"]]
        near_deadline_positive = [(row, state) for row, state in positive
                                  if group["expiry"] - 1 <= row["epoch"] < group["expiry"]]
        near_deadline_work = [(row, state) for row, state in near_deadline_positive
                              if any(value > 0 for key, value in state["lengths"].items()
                                     if key.startswith("physical_stub_"))]
        zeros = [(row, state) for row, state in rows if not any(state["lengths"].values())
                 and state["scheduler_running"] == state["scheduler_waiting"] == 0]
        first_zero = zeros[0][0] if zeros else None
        scans = [row for row, _ in rows if row["kind"] == "production_expiry_scan_before"
                 and row["supplied_epoch"] >= group["expiry"]]
        anchor = group["anchor"]
        deadline_checks = [
            {key: row[key] for key in ("attempt_no", "selected_deadline_loop",
                                      "ttl_deadline_lower", "ttl_deadline_upper",
                                      "initial_lease_same", "lease_unchanged_during_call",
                                      "first_lease_deadline_loop", "actual_lease_deadline_loop",
                                      "selection_before", "selection_after")}
            for row, _ in rows if row["kind"] == "asr_deadline_selected"
            and row.get("session_id") == sid
        ]
        observed_jobs = [job for _, state in rows for job in state["jobs"]]
        final_clock = rows[-1][0]["observed_after"]
        drifts = [(row["epoch"] - anchor["epoch"])
                  - (row["monotonic_after"] - anchor["monotonic_after"]) for row, _ in rows]
        strict = ("fail_observed_pcm_after_expiry" if late_positive else
                  "pass_upper_bound_within_expiry" if first_zero and first_zero["observed_after"]["epoch"] <= group["expiry"]
                  else "not_proven_sub_tick_release_time")
        summary[group["name"]] = {
            "first_failure_epoch": group["expiry"] - 600,
            "expiry_epoch": group["expiry"], "configured_ttl_seconds": 600,
            "anchor": anchor, "target_monotonic": group["target_monotonic"],
            "retry_request": group["retry_request"],
            "last_positive": ({k: positive[-1][0][k] for k in
                               ("kind", "epoch", "monotonic_before", "monotonic_after")} if positive else None),
            "first_all_zero": ({k: first_zero[k] for k in
                                ("kind", "epoch", "monotonic_before", "monotonic_after", "observed_after")} if first_zero else None),
            "first_all_zero_lateness_ms": ((first_zero["observed_after"]["epoch"] - group["expiry"]) * 1000 if first_zero else None),
            "first_lease_deadline_loop": group["first_lease_deadline_loop"],
            "first_zero_upper_after_original_lease_ms": (
                (first_zero["observed_after"]["loop_after"]-group["first_lease_deadline_loop"])*1000
                if first_zero else None),
            "positive_observations_at_or_after_original_D_plus_1": sum(
                row["loop_before"] >= group["first_lease_deadline_loop"]+1 for row, _ in positive),
            "positive_observations_at_or_after_expiry": len(late_positive),
            "positive_observations_in_last_second": len(near_deadline_positive),
            "positive_physical_work_observations_in_last_second": len(near_deadline_work),
            "first_late_positive": late_positive[0][0] if late_positive else None,
            "first_eligible_scan_lateness_ms": ((scans[0]["epoch"] - group["expiry"]) * 1000 if scans else None),
            "epoch_vs_monotonic_drift_min_ms": min(drifts) * 1000,
            "epoch_vs_monotonic_drift_max_ms": max(drifts) * 1000,
            "observed_monotonic_seconds_after_anchor": rows[-1][0]["monotonic_before"] - anchor["monotonic_after"],
            "first_failure_monotonic_bounds": {
                "lower": group["first_failure_monotonic_lower"],
                "upper": group["first_failure_monotonic_upper"],
                "assumption": "epoch-minus-monotonic remains stable; inspect recorded drift and sampling gaps",
            },
            "elapsed_from_first_failure_lower_seconds": final_clock["monotonic_before"] - group["first_failure_monotonic_upper"],
            "elapsed_from_first_failure_upper_seconds": final_clock["monotonic_after"] - group["first_failure_monotonic_lower"],
            "deadline_selections": deadline_checks,
            "observed_retry_jobs": observed_jobs,
            "strict_600_seconds": strict,
        }
    return summary


def test_cap15_real_600s_idle_and_599s_inflight_retry(real600_client):
    env = real600_client
    client, observer, control = env.client, env.observer, env.control
    source_before = source_manifest()
    outcomes = {"kind": "real_elapsed_600s_synthetic_asr_control", "completed": False,
                "source_before": source_before}
    idle_headers = auth_headers(register_and_login(client, email="ttl-idle@example.invalid"))
    retry_headers = auth_headers(register_and_login(client, email="ttl-retry@example.invalid"))

    async def flush_progress(start):
        # Low-frequency append-only checkpoint. No I/O is in production cleanup
        # callbacks, the 599-second launch, or the final boundary interval.
        if len(observer.rows) <= start:
            return start
        stop = len(observer.rows)
        payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in observer.rows[start:stop])
        def append():
            with (env.directory / "timeline.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(payload)
        await asyncio.to_thread(append)
        return stop

    async def run():
        app, manager = client.app, client.app.state.sessions
        flushed = 0
        request_task = None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
                idle = await failed_upload(http, app, idle_headers, control, 8000)
                observer.add("idle", idle)
                idle_facts = await permanent_facts(app, idle)
                retry = await failed_upload(http, app, retry_headers, control, 8000)
                observer.add("retry_at_599", retry)
                retry_facts = await permanent_facts(app, retry)
                retry_group = observer.groups[retry.sid]
                target = retry_group["target_monotonic"] - 1
                gate = PhysicalGate()
                control.gates[(retry.sid, 2)] = gate
                last_checkpoint = time.monotonic()
                finish_observation = max(g["target_monotonic"] for g in observer.groups.values()) + 2
                # 602 seconds is only an observation endpoint, NOT a TTL grace.
                # Product validity remains strict first_failure + 600 throughout.
                async with asyncio.timeout(615):
                    while time.monotonic() < finish_observation:
                        now = time.monotonic()
                        observer.record("sample")
                        if request_task is None and now >= target:
                            assert len(retry.audio) == 16000
                            assert (await detail(http, retry_headers, retry.sid))["retry_kind"] == "asr"
                            request_id = str(uuid.uuid4())
                            async def start_retry():
                                retry_group["retry_request"] = clock_sample()
                                assert retry_group["retry_request"]["epoch"] < retry.expiry, "test launch missed the original TTL"
                                response = await http.post(f"/api/v1/sessions/{retry.sid}/retry",
                                    headers={**retry_headers, "Idempotency-Key": request_id},
                                    json={"retry_kind": "asr"})
                                outcomes["retry_response"] = {"status": response.status_code,
                                    "attempt": response.json().get("current_attempt_no")}
                                assert response.status_code == 202
                                assert response.json()["current_attempt_no"] == 2
                            request_task = asyncio.create_task(start_retry())
                        # Collect failures after the original boundary; never
                        # trigger cleanup or release the fake worker early.
                        if now - last_checkpoint >= 30 and now < target - 3:
                            flushed = await flush_progress(flushed)
                            last_checkpoint = time.monotonic()
                        remaining = min(abs(g["target_monotonic"] - now) for g in observer.groups.values())
                        await asyncio.sleep(.02 if remaining < 2 or now >= target - 1 else min(1, max(.02, target - now)))
                if request_task is not None:
                    async with asyncio.timeout(3):
                        await request_task
                observer.record("after_boundary_observation")
                outcomes["physical_retry_started"] = gate.entered.is_set()
                outcomes["physical_retry_cancelled"] = gate.cancelled.is_set()
                outcomes["physical_retry_returned"] = gate.returned.is_set()
                outcomes["permanent_facts"] = {}
                for name, item, headers, expected_facts in [
                    ("idle", idle, idle_headers, idle_facts),
                    ("retry_at_599", retry, retry_headers, retry_facts),
                ]:
                    current = await detail(http, headers, item.sid)
                    outcomes[name] = {"state": current["state"], "attempt": current["current_attempt_no"],
                        "retry_kind": current["retry_kind"], "reason": current["retry_unavailable_reason"],
                        "received_samples": current["received_samples"], "result_present": current["result"] is not None,
                        "error_code": current["error_code"]}
                    denied = await http.post(f"/api/v1/sessions/{item.sid}/retry",
                        headers={**headers, "Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "asr"})
                    outcomes[name]["retry_after_expiry"] = {"status": denied.status_code,
                        "code": denied.json().get("error", {}).get("code"),
                        "reason": denied.json().get("error", {}).get("details", {}).get("reason")}
                    row = await read_session(app, item.runtime.user_id, item.sid)
                    outcomes[name]["cache_status"] = row["retry_cache_status"]
                    actual_facts = await permanent_facts(app, item)
                    outcomes["permanent_facts"][name] = {"before": expected_facts, "after": actual_facts,
                                                       "unchanged": expected_facts == actual_facts}
                outcomes["completed"] = True
        finally:
            try:
                observer.record("observation_finally_before_fixture_cleanup")
                outcomes["groups"] = summarize(observer)
                outcomes["source_after"] = source_manifest()
                outcomes["source_unchanged"] = outcomes["source_before"] == outcomes["source_after"]
                async with asyncio.timeout(10):
                    await flush_progress(flushed)
                    payload = json.dumps(outcomes, indent=2, sort_keys=True) + "\n"
                    await asyncio.to_thread((env.directory / "summary.json").write_text, payload, encoding="utf-8")
            finally:
                # Evidence-write/cancellation failures must not strand a gate or
                # HTTP task. These effects occur after the final observation and
                # never become evidence of production expiry. Errors propagate.
                for gate in control.gates.values():
                    gate.release.set()
                if request_task is not None:
                    if not request_task.done():
                        request_task.cancel()
                    async with asyncio.timeout(5):
                        await asyncio.gather(request_task, return_exceptions=True)

    client.portal.call(run)
    assert outcomes["completed"] and outcomes["source_unchanged"]
    assert outcomes["physical_retry_started"] and outcomes["physical_retry_cancelled"] and outcomes["physical_retry_returned"]
    retry_group = outcomes["groups"]["retry_at_599"]
    assert retry_group["positive_physical_work_observations_in_last_second"] > 0
    checks = retry_group["deadline_selections"]
    assert checks and all(check["attempt_no"] == 2 for check in checks)
    # In this last-second retry the original lease is tighter than finish10/total30.
    # Do not re-map a wall clock 599 seconds later: its observed drift is evidence,
    # not permission to move the first failure's already-frozen monotonic lease.
    assert all(check["initial_lease_same"] and check["lease_unchanged_during_call"]
               and check["selected_deadline_loop"] == check["actual_lease_deadline_loop"]
               == check["first_lease_deadline_loop"] for check in checks), checks
    jobs = retry_group["observed_retry_jobs"]
    assert jobs and all(job["attempt_no"] == 2 for job in jobs)
    assert all(any(job["deadline_loop"] == check["selected_deadline_loop"] for check in checks)
               for job in jobs), {"jobs": jobs, "selections": checks}
    for name in ("idle", "retry_at_599"):
        result = outcomes[name]
        assert result["state"] == "failed" and result["received_samples"] == 8000 and not result["result_present"]
        assert result["retry_kind"] == "none" and result["reason"] == "audio_expired"
        assert result["cache_status"] == "expired"
        assert result["attempt"] == (1 if name == "idle" else 2)
        assert result["error_code"] == ("ASR_FAILED" if name == "idle" else "ASR_TIMEOUT")
        assert result["retry_after_expiry"] == {"status": 409, "code": "RETRY_UNAVAILABLE", "reason": "audio_expired"}
        assert outcomes["permanent_facts"][name]["unchanged"]
        assert outcomes["groups"][name]["elapsed_from_first_failure_lower_seconds"] >= 600
        assert outcomes["groups"][name]["positive_observations_in_last_second"] > 0
        # Current BR-05: eligibility/outcomes expire at D, local PCM by D+1.
        # Retain the stricter old-D diagnostic; never grant outcomes that second.
        group = outcomes["groups"][name]
        assert group["first_zero_upper_after_original_lease_ms"] is not None
        assert group["first_zero_upper_after_original_lease_ms"] <= 1000, group
        assert group["positive_observations_at_or_after_original_D_plus_1"] == 0, group
