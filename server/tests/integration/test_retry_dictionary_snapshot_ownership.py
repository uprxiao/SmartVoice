"""CAP15 event-order regressions: real PG/ASGI, declared Mock physical work.

R4 also targets pre-lease QA. R3 targets lease v2. No clock override, manual
terminal-row writes, fake task.done, model quality or real-600-second claim.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.config import Config
from app.providers.asr.base import ProviderError
from app.providers.llm.mock import MockLlmProvider
from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_admission_atomicity import _wait_ready
from tests.integration.test_normal_shutdown_contract import WsProbe, create_session, require_owned_database
from tests.test_dictionary import create_term


@pytest.fixture
def ownership_client(monkeypatch, request):
    from app import main
    original_load = main.load_config

    def configured(path=None):
        original = original_load(path)
        values = original.model_dump()
        # A legal deployment count boundary, chosen before app startup. No
        # changing a running cache, byte reserve, expiry or cleanup clock.
        values["retry"].update(max_cached_sessions=1, audio_ttl_seconds=600)
        validated = Config.model_validate(values)
        return original.model_copy(update={"retry": validated.retry})

    monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    monkeypatch.setattr(main, "load_config", configured)
    return request.getfixturevalue("app_client")


async def until(predicate, seconds=5):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.01)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


async def stored(app, owner, sid):
    async with app.state.db.session_factory() as db:
        row = (await db.execute(text("""SELECT state,error_code,current_attempt_no,
            received_samples,audio_complete,first_asr_failed_at,retry_cache_status,
            current_result_id,llm_hints,dictionary_version FROM sessions
            WHERE user_id=:u AND id=:s"""), {"u": owner, "s": sid})).mappings().one()
        contribution = (await db.execute(text("""SELECT outcome,has_result,
            successful_audio_samples,received_samples FROM session_contributions
            WHERE user_id=:u AND session_id=:s"""), {"u": owner, "s": sid})).one()
        attempts = (await db.execute(text("""SELECT attempt_no,state,error_code
            FROM session_attempts WHERE user_id=:u AND session_id=:s ORDER BY attempt_no"""),
            {"u": owner, "s": sid})).all()
        return {"session": dict(row), "contribution": tuple(contribution),
                "attempts": [tuple(item) for item in attempts]}


async def wait_cache(app, owner, sid, status):
    async with asyncio.timeout(5):
        while True:
            value = await stored(app, owner, sid)
            if value["session"]["retry_cache_status"] == status:
                return value
            await asyncio.sleep(.2)


class PhysicalGates:
    def __init__(self, app, monkeypatch):
        self.app = app
        self.target = None
        self.old_cancel_entered, self.old_cancel_release = asyncio.Event(), asyncio.Event()
        self.old_cancel_returned = asyncio.Event()
        self.retry_finish_entered, self.retry_finish_release = asyncio.Event(), asyncio.Event()
        self.old_cancel_interruptions = 0
        self.provider_close_calls = 0
        self.streams, self.feeds, self.opened, self.refine_inputs = {}, {}, [], []
        self.raw = "请打开 Kubernetes 控制台。"
        actual_open = app.state.asr.open
        actual_generate = MockLlmProvider.generate
        actual_provider_close = app.state.asr.close

        async def opened(options):
            stream = await actual_open(options)
            key = options.session_id, options.attempt_no
            self.opened.append((options.user_id, *key))
            self.streams[key], self.feeds[key] = stream, []
            stream.text = self.raw
            original_feed, original_finish, original_cancel = stream.feed, stream.finish, stream.cancel

            async def feed(pcm, start_sample):
                self.feeds[key].append((start_sample, len(pcm), hashlib.sha256(pcm).hexdigest()))
                await original_feed(pcm, start_sample)

            async def finish(total_samples, deadline):
                if options.attempt_no == 1:
                    raise ProviderError("ASR_FAILED", "synthetic complete-audio fixture failure")
                if options.session_id == self.target and options.attempt_no == 2:
                    self.retry_finish_entered.set()
                    async with asyncio.timeout_at(deadline):
                        await self.retry_finish_release.wait()
                return await original_finish(total_samples, deadline)

            async def cancel():
                if options.session_id == self.target and options.attempt_no == 1:
                    self.old_cancel_entered.set()
                    try:
                        await self.old_cancel_release.wait()
                    except asyncio.CancelledError:
                        self.old_cancel_interruptions += 1
                        raise
                    await original_cancel()
                    self.old_cancel_returned.set()
                else:
                    await original_cancel()

            stream.feed, stream.finish, stream.cancel = feed, finish, cancel
            return stream

        async def generate(provider, request, deadline):
            scope = current_scope.get()
            if request.purpose == "refine" and scope and scope.session_id == self.target and scope.attempt_no == 2:
                self.refine_inputs.append(copy.deepcopy(request.input))
            return await actual_generate(provider, request, deadline)

        async def provider_close():
            self.provider_close_calls += 1
            return await actual_provider_close()

        monkeypatch.setattr(app.state.asr, "open", opened)
        monkeypatch.setattr(app.state.asr, "close", provider_close)
        monkeypatch.setattr(MockLlmProvider, "generate", generate)


async def open_failed_attempt(http, app, headers, gates, wires, *, target=False):
    response = await create_session(http, headers)
    assert response.status_code == 201
    sid = response.json()["session_id"]
    if target:
        gates.target = sid
    runtime = app.state.sessions.runtimes[sid]
    wire = WsProbe(app, headers, sid)
    wires.append(wire)  # Own it before the first await that can fail.
    await wire.start()
    await wire.end()
    return sid, runtime, wire


async def disconnect_after_end(wire):
    await wire.send({"type": "websocket.disconnect", "code": 1000})
    async with asyncio.timeout(5):
        await wire.task


# The second scenario targets the unadopted lease candidate and is tested in its own lab.
@pytest.mark.parametrize("scenario", ["retry_dictionary_snapshot"])
def test_tc_cap_15_old_failed_cleanup_owns_only_its_attempt(
    ownership_client, monkeypatch, record_property, scenario,
):
    client = ownership_client
    require_owned_database()
    actor = register_and_login(client, email="cleanup-old@example.invalid")
    owner, headers = actor["user"]["id"], auth_headers(actor)
    second = register_and_login(client, email="cleanup-eviction@example.invalid") if scenario == "evicted_cleanup_close" else None
    if scenario == "retry_dictionary_snapshot":
        term_response = create_term(client, headers, "Kubernetes", ["k8s"])
        assert term_response.status_code == 201
        term = term_response.json()
        assert term["dictionary_version"] == 1
        dictionary_before = client.get("/api/v1/dictionary/terms", headers=headers).json()

    async def run():
        app, manager = client.app, client.app.state.sessions
        assert manager.config.retry.max_cached_sessions == 1
        assert manager.config.retry.audio_ttl_seconds == 600
        assert manager.config.asr.finish_timeout_ms == 10000
        assert manager.config.pipeline.stop_to_result_budget_ms == 30000
        gates = PhysicalGates(app, monkeypatch)
        evidence = {"scenario": scenario, "scope": "real-PG-ASGI; synthetic PCM and Mock provider gates",
                    "ttl_seconds": 600, "cache_count_limit": 1, "issues": []}
        wires, owned_tasks = [], []
        old = new = close_task = drain_waiter = None
        original_drain = manager._drain_runtime
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ownership-test") as http:
                sid, old, wire = await open_failed_attempt(http, app, headers, gates, wires, target=True)
                old_task, original_audio = old.task, old.audio
                assert old_task is not None
                owned_tasks.append(old_task)
                await until(gates.old_cancel_entered.is_set)
                before = await stored(app, owner, sid)
                assert before["session"]["state"] == old.state == "failed"
                assert before["session"]["audio_complete"] and before["session"]["received_samples"] == 4000
                assert before["session"]["retry_cache_status"] == "available"
                assert len(original_audio) == 8000 and not old_task.done()
                assert old.asr is None and not gates.old_cancel_returned.is_set()
                first_expiry = old.retry_expires_at
                assert first_expiry == before["session"]["first_asr_failed_at"].timestamp() + 600
                await disconnect_after_end(wire)
                assert not old.connected and not old_task.done()
                current = await http.get(f"/api/v1/sessions/{sid}", headers=headers)
                assert current.status_code == 200 and current.json()["retry_kind"] == "asr"

                if scenario == "retry_dictionary_snapshot":
                    frozen = copy.deepcopy(old.dictionary_snapshot)
                    assert len(frozen) == 1 and frozen[0]["id"] == term["id"]
                    assert frozen[0]["term"] == "Kubernetes" and frozen[0]["aliases"] == ["k8s"]
                    retry = await http.post(f"/api/v1/sessions/{sid}/retry",
                        headers={**headers, "Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "asr"})
                    assert retry.status_code == 202 and retry.json()["current_attempt_no"] == 2
                    new = manager.runtimes[sid]
                    assert new is not old and new.audio is original_audio and old.audio == b""
                    assert new.retry_expires_at == first_expiry
                    assert new.dictionary_snapshot == frozen and new.dictionary_version == 1
                    owned_tasks.append(new.task)
                    await until(gates.retry_finish_entered.is_set)
                    evidence["shared_snapshot_before_old_cleanup"] = new.dictionary_snapshot is old.dictionary_snapshot
                    gates.old_cancel_release.set()
                    async with asyncio.timeout(5):
                        await old_task
                    assert gates.old_cancel_returned.is_set() and gates.old_cancel_interruptions == 0
                    evidence.update(snapshot_before_sha256=digest(frozen),
                        snapshot_after_sha256=digest(new.dictionary_snapshot),
                        snapshot_before_count=len(frozen), snapshot_after_count=len(new.dictionary_snapshot))
                    if new.dictionary_snapshot != frozen:
                        evidence["issues"].append("old cleanup changed new frozen dictionary snapshot")
                    gates.retry_finish_release.set()
                    final = await _wait_ready(http, headers, sid)
                    assert final["current_attempt_no"] == 2 and final["result"]["dictionary_version"] == 1
                    assert final["result"]["raw_text"] == gates.raw
                    assert gates.feeds[(sid, 1)] == gates.feeds[(sid, 2)]
                    after = await stored(app, owner, sid)
                    actual_inputs = [entry["confirmed_terms"] for entry in gates.refine_inputs]
                    assert all(set(entry) == {"transcript", "confirmed_terms"} for entry in gates.refine_inputs)
                    evidence["refine_input_term_counts"] = [len(terms) for terms in actual_inputs]
                    evidence["stored_llm_hint_count"] = len(after["session"]["llm_hints"] or [])
                    expected = {key: frozen[0][key] for key in ("id", "term", "norm", "aliases")}
                    expected.update(contexts=[], ambiguous_alias_norms=[])
                    if actual_inputs != [[expected["term"]]]:
                        evidence["issues"].append("actual R2 request lost frozen canonical term")
                    if after["session"]["llm_hints"] != [expected]:
                        evidence["issues"].append("persisted refine hints lost frozen term identity or aliases")
                    dictionary_after = await http.get("/api/v1/dictionary/terms", headers=headers)
                    assert dictionary_after.status_code == 200 and dictionary_after.json() == dictionary_before
                    assert after["session"]["state"] == "ready" and after["session"]["dictionary_version"] == 1
                    assert [item[0] for item in after["attempts"]] == [1, 2]
                else:
                    assert hasattr(old, "audio_lease"), "R3 targets lease v2; use only R4 on the pre-lease source"
                    other_headers, other_id = auth_headers(second), second["user"]["id"]
                    other_sid, other, other_wire = await open_failed_attempt(http, app, other_headers, gates, wires)
                    owned_tasks.append(other.task)
                    other_wire.drain_task = asyncio.create_task(other_wire.drain())
                    owned_tasks.append(other_wire.drain_task)
                    async with asyncio.timeout(5):
                        await other_wire.drain_task
                    assert other_wire.events[-1]["type"] == "error"
                    assert other_wire.events[-1]["error"]["code"] == "ASR_FAILED"
                    evicted = await wait_cache(app, owner, sid, "evicted")
                    await until(lambda: old.cache_cleanup_task is None)
                    assert original_audio == b"" and old.retry_expires_at is None
                    assert not old_task.done() and not gates.old_cancel_returned.is_set()
                    assert gates.old_cancel_interruptions == 0
                    assert evicted["contribution"] == before["contribution"]
                    assert evicted["attempts"] == before["attempts"]
                    assert evicted["session"]["first_asr_failed_at"] == before["session"]["first_asr_failed_at"]
                    kept = await stored(app, other_id, other_sid)
                    assert kept["session"]["retry_cache_status"] == "available" and len(other.audio) == 8000
                    evidence["old_runtime_in_map_after_eviction"] = manager.runtimes.get(sid) is old
                    drain_entered = asyncio.Event()

                    async def observed_drain(runtime):
                        if runtime is old:
                            drain_entered.set()
                        return await original_drain(runtime)

                    manager._drain_runtime = observed_drain  # Observe; original join remains unchanged.

                    async def close_manager():
                        await manager.close(graceful=True)
                        evidence["old_task_done_at_manager_close_return"] = old_task.done()
                        evidence["provider_cancel_returned_at_manager_close_return"] = gates.old_cancel_returned.is_set()
                        evidence["provider_close_calls_at_manager_close_return"] = gates.provider_close_calls

                    close_task = asyncio.create_task(close_manager())
                    drain_waiter = asyncio.create_task(drain_entered.wait())
                    owned_tasks.extend((close_task, drain_waiter))
                    done, _ = await asyncio.wait({close_task, drain_waiter}, timeout=5,
                                                return_when=asyncio.FIRST_COMPLETED)
                    assert done, "neither close returned nor original drain joined the old runtime; inspect fixture/owner path"
                    if close_task in done:
                        await close_task
                    else:
                        assert drain_entered.is_set() and not close_task.done() and not old_task.done()
                    evidence["old_drain_join_observed"] = drain_entered.is_set()
                    gates.old_cancel_release.set()
                    async with asyncio.timeout(5):
                        await asyncio.gather(old_task, close_task)
                    if not evidence["old_task_done_at_manager_close_return"]:
                        evidence["issues"].append("manager.close returned before evicted old cleanup task completed")
                    if not evidence["provider_cancel_returned_at_manager_close_return"]:
                        evidence["issues"].append("manager.close returned before old provider cancel confirmation")
                    assert evidence["provider_close_calls_at_manager_close_return"] == 0
                    assert gates.old_cancel_interruptions == 0
                    assert old_task.done() and gates.old_cancel_returned.is_set()
                    final = await stored(app, owner, sid)
                    assert final["attempts"] == before["attempts"] and final["contribution"] == before["contribution"]
                    assert final["session"]["current_result_id"] is None
                    # This explicit manager.close has not invoked provider.close;
                    # a later lifespan-wide provider cleanup cannot hide its result.
                assert not evidence["issues"], evidence["issues"]
        finally:
            gates.old_cancel_release.set()
            gates.retry_finish_release.set()
            cleanup_errors = []
            try:
                for wire in wires:
                    if wire.task is not None and not wire.task.done():
                        try:
                            await disconnect_after_end(wire)
                        except BaseException as exc:
                            cleanup_errors.append({"stage": "disconnect", "type": type(exc).__name__})
                tasks = set(task for task in owned_tasks if task is not None)
                tasks.update(wire.task for wire in wires if wire.task is not None)
                if drain_waiter is not None and not drain_waiter.done():
                    drain_waiter.cancel()
                if tasks:
                    done, pending = await asyncio.wait(tasks, timeout=8)
                    if pending:
                        cleanup_errors.append({"stage": "join", "type": "owned tasks still pending"})
                        for task in pending:
                            if not task.cancelling():
                                task.cancel()
                        _, still_pending = await asyncio.wait(pending, timeout=3)
                        if still_pending:
                            cleanup_errors.append({"stage": "cancel_join", "type": "owned tasks still pending"})
                    await asyncio.gather(*(task for task in tasks if task.done()), return_exceptions=True)
            finally:
                manager._drain_runtime = original_drain
                evidence["cleanup_errors"] = cleanup_errors
                evidence["old_cancel_interruptions"] = gates.old_cancel_interruptions
                record_property("cap15_cleanup_ownership", json.dumps(evidence, sort_keys=True))
            assert not cleanup_errors, cleanup_errors

    client.portal.call(run)
