"""CAP13 formal PG/API/WS queue expiry; physical ASR/LLM remain Mock.

Real production scheduler/recorder/services run unchanged. No clock replacement,
shortened wait, fake session state, synthetic CallScope, or online API is added.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select, text

from app.core.config import Config
from app.models import ModelCall
from app.services.inference_context import current_scope
from app.services.inference_scheduler import SchedulerBusy
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_normal_completion import AsgiPcmStream
from tests.test_insertions_learning import claim, receipt
from tests.test_retry import wait_state
from tests.test_sessions import create_session, dictation, frame


@pytest.fixture
def queue_client(monkeypatch, request):
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    from app import main as main_module

    load = main_module.load_config

    def one_slot(path=None):
        base = load(path)
        payload = base.model_dump()
        payload["llm"]["max_inflight"] = 1  # Valid deployment size, not a time/queue-budget override.
        checked = Config.model_validate(payload)
        return base.model_copy(update={"llm": checked.llm})  # Preserve private DB/secret fields.

    monkeypatch.setattr(main_module, "load_config", one_slot)
    return request.getfixturevalue("app_client")


async def _ledger(app, owner):
    answer = {}
    async with app.state.db.session_factory() as db:
        for table in ("model_calls", "model_usage_daily"):
            rows = list((await db.scalars(text(
                f"SELECT to_jsonb(t) FROM {table} t WHERE user_id=:u ORDER BY to_jsonb(t)::text"
            ), {"u": owner})).all())
            encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            answer[table] = {"count": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
    return answer


async def _calls(app, owner, session_id):
    async with app.state.db.session_factory() as db:
        rows = (await db.execute(select(ModelCall.attempt_no, ModelCall.purpose,
            ModelCall.status, ModelCall.learning_event_id).where(
                ModelCall.user_id == owner, ModelCall.session_id == session_id)
            .order_by(ModelCall.attempt_no, ModelCall.purpose, ModelCall.call_no))).all()
        return [tuple(row) for row in rows]


class QueueObservation:
    """Observe a real queued job before and on its original expiry timer tick.

    Assertions execute in futures awaited by the test, never as unhandled event-
    loop callback errors. None of the callbacks mutates a production job/clock.
    """

    def __init__(self, scheduler, label, owner, priority, entered, deadline, wait_deadline):
        self.scheduler, self.label, self.owner, self.priority = scheduler, label, owner, priority
        self.entered, self.deadline, self.wait_deadline = entered, deadline, wait_deadline
        self.budget_ms = 2000 if label == "foreground" else 1000
        self.loop = asyncio.get_running_loop()
        self.before, self.due = self.loop.create_future(), self.loop.create_future()
        self.handles, self.job = [], None
        self.returned, self.busy = None, False
        # The real ScheduledLlmProvider computed this before submit; no extra
        # delay is added here, even when the attempt has much more time left.
        assert 0 < (wait_deadline - entered) * 1000 <= self.budget_ms
        assert wait_deadline < deadline
        self.monitor = asyncio.create_task(self._observe_job())

    async def _observe_job(self):
        async with asyncio.timeout(.25):
            while self.owner not in self.scheduler.queues[self.priority]:
                await asyncio.sleep(0)
        self.job = self.scheduler.queues[self.priority][self.owner]
        assert self.job.wait_deadline == self.wait_deadline
        assert self.job.deadline == self.deadline
        assert self.loop.time() < self.wait_deadline - .05
        self.handles.append(self.loop.call_at(self.wait_deadline - .05, self._probe, False))
        # Equal-time asyncio timers have unspecified ordering. call_soon makes
        # the assertion run after all callbacks due at the SAME deadline tick;
        # it does not postpone/replace production's queue-expiration timer.
        self.handles.append(self.loop.call_at(self.wait_deadline, self._deadline_tick))

    def _deadline_tick(self):
        self.handles.append(self.loop.call_soon(self._probe, True))

    def _probe(self, expired):
        future = self.due if expired else self.before
        if future.done():
            return
        try:
            now = self.loop.time()
            job = self.job
            assert job is not None and job.started is False
            if not expired:
                assert self.scheduler.queues[self.priority].get(self.owner) is job
                assert not job.future.done() and job.operation is not None
            else:
                assert self.owner not in self.scheduler.queues[self.priority]
                assert job.future.done() and not job.future.cancelled()
                assert isinstance(job.future.exception(), SchedulerBusy)
                assert job.done.is_set() and job.operation is None
            future.set_result({"elapsed_ms": (now - self.entered) * 1000,
                "timer_lateness_ms": (now - (self.wait_deadline if expired else self.wait_deadline - .05)) * 1000})
        except Exception as exc:
            future.set_exception(exc)

    async def evidence(self):
        async with asyncio.timeout(4):
            await self.monitor
            before, due = await asyncio.gather(self.before, self.due)
        assert self.busy and self.returned is not None
        return {"budget_ms": self.budget_ms,
            "scheduled_wait_ms": (self.wait_deadline - self.entered) * 1000,
            "observed_return_ms": (self.returned - self.entered) * 1000,
            "pre_expiry": before, "expiry_tick": due,
            "started": self.job.started, "payload_released": self.job.operation is None}

    async def close(self):
        for handle in self.handles:
            handle.cancel()
        if not self.monitor.done():
            self.monitor.cancel()
        for future in (self.before, self.due):
            if not future.done():
                future.cancel()
        await asyncio.gather(self.monitor, self.before, self.due, return_exceptions=True)


class ControlledQueue:
    def __init__(self, app, monkeypatch, blocker_sid, foreground_owner, learning_owner):
        self.app, self.llm = app, app.state.llm
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.observations, self.physical_calls, self.stream = {}, [], None
        original_generate, original_submit = self.llm.inner.generate, self.llm.scheduler.submit

        async def physical(request, deadline):
            scope = current_scope.get()
            assert scope is not None and scope.synthetic is False and request.call_id
            self.physical_calls.append((scope.user_id, scope.session_id, scope.attempt_no,
                                        scope.learning_event_id, request.purpose))
            if scope.session_id == blocker_sid:
                assert request.purpose == "refine"
                self.entered.set()
                await self.release.wait()  # Physical Mock boundary; scheduler/recorder unchanged.
            return await original_generate(request, deadline)

        async def submit(owner, priority, operation, *, deadline, wait_deadline=None, replace=False):
            scope = current_scope.get()
            label = ("foreground" if owner == foreground_owner else
                     "classification" if owner == learning_owner else None)
            observed = None
            if label is not None:
                assert scope is not None and not scope.synthetic
                assert (priority, bool(scope.learning_event_id)) == (
                    ("high", False) if label == "foreground" else ("low", True))
                assert label not in self.observations, "busy response was silently retried"
                observed = QueueObservation(self.llm.scheduler, label, owner, priority,
                    asyncio.get_running_loop().time(), deadline, wait_deadline)
                self.observations[label] = observed
            try:
                return await original_submit(owner, priority, operation, deadline=deadline,
                    wait_deadline=wait_deadline, replace=replace)
            except SchedulerBusy:
                if observed:
                    observed.busy = True
                raise
            finally:
                if observed:
                    observed.returned = asyncio.get_running_loop().time()

        monkeypatch.setattr(self.llm.inner, "generate", physical)
        monkeypatch.setattr(self.llm.scheduler, "submit", submit)

    async def start_blocker(self, headers, sid, raw):
        self.stream = AsgiPcmStream(self.app, SimpleNamespace(headers=lambda: headers), sid, 9, raw)
        await self.stream.start()
        self.stream.finish_requested.set()
        async with asyncio.timeout(3):
            await self.entered.wait()
        assert self.llm.scheduler.snapshot() == {"running": 1, "high_waiting": 0, "low_waiting": 0}

    async def verify_waiters(self):
        assert set(self.observations) == {"foreground", "classification"}
        evidence = {label: await observation.evidence() for label, observation in self.observations.items()}
        assert not self.release.is_set()  # A freed permit cannot explain either timeout.
        assert self.llm.scheduler.snapshot() == {"running": 1, "high_waiting": 0, "low_waiting": 0}
        return evidence

    async def complete_blocker(self):
        self.release.set()
        async with asyncio.timeout(4):
            await self.stream.runner  # Normal audio.end/result/close; do not cancel to free the slot.
        assert self.llm.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}

    async def close(self):
        self.release.set()
        if self.stream is not None:
            # Release the physical fixture first, so error cleanup need not
            # consume the product's conservative remote-cancellation deadline.
            if self.stream.runner is not None and not self.stream.runner.done():
                try:
                    async with asyncio.timeout(4):
                        await asyncio.shield(self.stream.runner)
                except Exception:
                    pass  # The main assertion remains the failure; dispose collects both tasks.
            await self.stream.dispose()
        for observation in self.observations.values():
            await observation.close()


def _learning_result(client, headers, event_id):
    deadline = time.monotonic() + 5  # Bounded HTTP observation only, not a model deadline.
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/learning-events/{event_id}", headers=headers)
        assert response.status_code == 200
        if response.json()["status"] != "pending":
            return response.json()
        time.sleep(.2)  # At most 25 reads; preserve the normal per-user rate limiter.
    raise AssertionError("learning event did not publish its queue failure")


def test_tc_cap_13_real_wait_deadlines_publish_fallback_and_failed_without_model_calls(
    queue_client, monkeypatch, record_property, caplog,
):
    caplog.set_level(logging.WARNING)
    client, raw = queue_client, "请用 SQL lite 保存项目配置。"
    app, config = client.app, client.app.state.config
    assert (config.llm.max_inflight, config.llm.foreground_wait_ms, config.llm.classify_wait_ms,
            config.llm.call_timeout_ms, config.pipeline.stop_to_result_budget_ms,
            config.pipeline.classification_budget_ms) == (1, 2000, 1000, 10000, 30000, 10000)
    assert app.state.sessions.refiner.llm is app.state.classifier.llm is app.state.llm
    assert app.state.llm.recorder is app.state.recorder and app.state.recorder is not None
    monkeypatch.setattr(app.state.asr, "text", raw)  # Deterministic legal audio fixture transcript.
    learner = register_and_login(client, email="cap13-learning@example.invalid")
    blocker = register_and_login(client, email="cap13-blocker@example.invalid")
    foreground = register_and_login(client, email="cap13-foreground@example.invalid")
    lh, bh, fh = [auth_headers(actor) for actor in (learner, blocker, foreground)]
    lu, bu, fu = [actor["user"]["id"] for actor in (learner, blocker, foreground)]

    seeded = create_session(client, lh, str(uuid.uuid4()))
    assert seeded.status_code == 201
    source_sid = seeded.json()["session_id"]
    source = dictation(client, lh, source_sid)[-1]
    assert source["type"] == "result.ready" and source["result"]["review_required"] is False
    assert source["result"]["refined_text"] == raw
    claimed = claim(client, lh, source_sid, source["result"])
    assert claimed.status_code == 201
    insertion_id = claimed.json()["insertion_id"]
    inserted = receipt(client, lh, source_sid, insertion_id, "inserted", True)
    assert inserted.status_code == 200 and inserted.json()["observable"] is True
    # A synthetic API client receipt is test context, never native insertion proof.
    before = {owner: client.portal.call(_ledger, app, owner) for owner in (lu, fu)}
    assert before[lu]["model_calls"]["count"] == 1 and before[fu]["model_calls"]["count"] == 0
    assert client.portal.call(_calls, app, lu, source_sid) == [(1, "refine", "succeeded", None)]
    dictionary_before = client.get("/api/v1/dictionary/terms", headers=lh)
    assert dictionary_before.status_code == 200

    blocked = create_session(client, bh, str(uuid.uuid4()))
    front = create_session(client, fh, str(uuid.uuid4()))
    assert blocked.status_code == front.status_code == 201
    blocker_sid, front_sid = blocked.json()["session_id"], front.json()["session_id"]
    control = client.portal.call(ControlledQueue, app, monkeypatch, blocker_sid,
                                 (fu, front_sid, 1), (lu, "classify"))
    try:
        client.portal.call(control.start_blocker, bh, blocker_sid, raw)
        assert client.portal.call(_calls, app, bu, blocker_sid) == [(1, "refine", "running", None)]
        with client.websocket_connect(f"/ws/v1/sessions/{front_sid}/audio", headers=fh) as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(frame(0))
            ack = ws.receive_json()
            assert (ack["type"], ack["seq"], ack["total_samples"]) == ("audio.ack", 0, 4000)
            ws.send_json({"type": "audio.end", "last_seq": 0, "total_samples": 4000})
            assert ws.receive_json()["type"] == "audio.end_ack"
            final_asr = ws.receive_json()
            assert final_asr["type"] == "asr.final" and final_asr["text"] == raw
            offset, before_text = raw.index("SQL lite"), "SQL lite"
            submitted = client.post("/api/v1/corrections", headers=lh, json={
                "session_id": source_sid, "insertion_id": insertion_id,
                "correction_id": str(uuid.uuid4()), "offset": offset,
                "before": before_text, "after": "SQLite", "left": raw[:offset],
                "right": raw[offset + len(before_text):],
            })
            assert submitted.status_code == 202 and submitted.json()["status"] == "pending"
            event_id = submitted.json()["event_id"]
            terminal = ws.receive_json()
            terminal_identity = {"type": terminal["type"], "code": terminal.get("error", {}).get("code")}
            record_property("cap13_foreground_terminal", json.dumps(terminal_identity))
            assert terminal["type"] == "result.ready", terminal_identity
            result = terminal["result"]
            assert result["raw_text"] == result["refined_text"] == raw
            assert result["review_required"] is True and result["review_reason"] == "llm_unavailable"
        learning = _learning_result(client, lh, event_id)
        assert (learning["status"], learning["reason"]) == ("failed", "MODEL_BUSY")
        assert learning["classification"] is None and learning["term"] is None
        assert learning["undo_available"] is False
        evidence = client.portal.call(control.verify_waiters)
        assert control.physical_calls == [(bu, blocker_sid, 1, None, "refine")]
        assert {owner: client.portal.call(_ledger, app, owner) for owner in (lu, fu)} == before
        assert client.portal.call(_calls, app, fu, front_sid) == []
        dictionary_after = client.get("/api/v1/dictionary/terms", headers=lh)
        assert dictionary_after.status_code == 200 and dictionary_after.json() == dictionary_before.json()

        client.portal.call(control.complete_blocker)
        assert client.portal.call(_calls, app, bu, blocker_sid) == [(1, "refine", "succeeded", None)]
        # An explicit legal refine retry after the permit is free must dispatch
        # and record once, disproving a fixture with disabled provider/recorder.
        retried = client.post(f"/api/v1/sessions/{front_sid}/retry", headers={
            **fh, "Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "refine"})
        assert retried.status_code == 202
        recovered = wait_state(client, fh, front_sid)
        assert recovered["state"] == "ready" and recovered["current_attempt_no"] == 2
        assert recovered["result"]["review_required"] is False
        assert recovered["result"]["raw_text"] == recovered["result"]["refined_text"] == raw
        assert client.portal.call(_calls, app, fu, front_sid) == [(2, "refine", "succeeded", None)]
        assert control.physical_calls == [(bu, blocker_sid, 1, None, "refine"),
                                          (fu, front_sid, 2, None, "refine")]
        assert client.portal.call(_ledger, app, lu) == before[lu]
        record_property("cap13_actual_queue_waits", json.dumps(evidence, sort_keys=True))
        # S2 requires a diagnostic for a foreground MODEL_BUSY fallback too;
        # the classifier's persisted event alone cannot prove that observation.
        purposes = [purpose for purpose in ("refine", "classify") if any(
            "MODEL_BUSY" in record.getMessage() and purpose in record.getMessage()
            for record in caplog.records
        )]
        record_property("cap13_busy_diagnostic_purposes", json.dumps(purposes))
        assert purposes == ["refine", "classify"]
        busy_messages = [record.getMessage() for record in caplog.records if "MODEL_BUSY" in record.getMessage()]
        assert all(raw not in message and "SQL lite" not in message for message in busy_messages)
        assert all(actor["access_token"] not in message for actor in (learner, blocker, foreground)
                   for message in busy_messages)
    finally:
        client.portal.call(control.close)
