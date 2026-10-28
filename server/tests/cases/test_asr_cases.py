from __future__ import annotations

import pytest

from app.providers.asr.base import ProviderError
from app.providers.llm.base import LlmResponse
from app.services.refiner import RefinerService


class StubLlm:
    provider_id = "stub"
    model_id = "stub"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def prepare(self):
        return None

    async def health(self):
        return True

    async def close(self):
        return None

    async def generate(self, request, deadline):
        self.calls.append(request.purpose)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return LlmResponse(parsed=item, provider="stub", model_id="stub")
        return LlmResponse(
            parsed=None,
            provider="stub",
            model_id="stub",
            error=ProviderError(item, item),
        )


def response(text, omissions=None):
    return {"candidate_text": text, "edits": omissions or []}


def audit(decision):
    return {"decision": decision, "violations": [] if decision == "pass" else [{"category": "fact", "source_start": 0, "source_end": 1, "reason_code": "SWAPPED_OBJECT"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "omissions",
    [
        [{"kind": "filler", "source_start": 0, "source_end": 2, "source_text": "不存在", "replacement_text": ""}],
        [
            {"kind": "filler", "source_start": 0, "source_end": 4, "source_text": "重试 3", "replacement_text": ""},
            {"kind": "repetition", "source_start": 1, "source_end": 6, "source_text": "试 3 次", "replacement_text": "试 3 次"},
        ],
        [{"kind": "filler", "source_start": 7, "source_end": 8, "source_text": "不", "replacement_text": ""}],
    ],
)
async def test_tc_asr_07_invalid_omissions_fall_back_to_raw(omissions):
    raw = "重试 3 次，不要删除文件"
    stub = StubLlm([response("重试 5 次，删除文件", omissions), audit("pass")])
    result = await RefinerService(stub, budget_ms=5_000).refine(raw)
    assert result.review_required is True
    assert result.text == raw
    assert result.review_reason == "llm_invalid_output"
    assert stub.calls == ["refine"]


async def test_tc_asr_09_llm_timeout_falls_back():
    raw = "帮我加一个按钮"
    stub = StubLlm(["timeout"])
    result = await RefinerService(stub, budget_ms=5_000).refine(raw)
    assert result.review_required is True
    assert result.text == raw
    assert result.review_reason == "llm_timeout"
    assert stub.calls == ["refine"]


async def test_tc_asr_10_one_format_retry_then_fallback():
    raw = "帮我加一个按钮"
    stub = StubLlm(["invalid_output", "invalid_output"])
    result = await RefinerService(stub, budget_ms=5_000).refine(raw)
    assert result.review_required is True
    assert result.text == raw
    assert result.review_reason == "llm_invalid_output"
    assert stub.calls == ["refine", "refine"]


async def test_tc_asr_14_invalid_format_cannot_swap_objects():
    raw = "删除缓存文件，不要修改配置文件"
    candidate = "删除配置文件，不要修改缓存文件"
    # The removed LLM semantic judge is not replaced by a fake pass/fail.
    # Check the remaining deterministic evidence guard; real T10 quality has
    # separate actual-model evidence.
    proof={"kind":"format","source_start":0,"source_end":len(raw),
           "source_text":raw,"replacement_text":candidate}
    stub = StubLlm([response(candidate,[proof])])
    result = await RefinerService(stub, budget_ms=5_000).refine(raw)
    assert result.review_required is True
    assert result.text == raw
    assert result.review_reason == "llm_invalid_output"
    assert stub.calls == ["refine"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,reason", [
    ({}, "llm_invalid_output"), ("timeout", "llm_timeout"),
    ("refused", "llm_refused"), ("truncated", "llm_truncated"),
    (response("等待35秒，不要删除配置。"), "protected_content_changed"),
])
async def test_tc_asr_17_generation_failures_require_review_without_audit(fault, reason):
    raw = "等待3.5秒，不要删除配置。"
    stub = StubLlm([fault])
    result = await RefinerService(stub, budget_ms=5_000, automatic_retries=0).refine(raw)
    assert result.review_required and result.text == raw
    assert result.review_reason == reason and stub.calls == ["refine"]


async def test_tc_asr_18_layout_only_change_skips_audit():
    raw = "先检查权限 再开始录音"
    stub = StubLlm([response("先检查权限，再开始录音。")])
    result = await RefinerService(stub, budget_ms=5_000).refine(raw)
    assert result.review_required is False
    assert result.text == "先检查权限，再开始录音。"
    assert stub.calls == ["refine"]


async def test_tc_asr_18_semantic_rewrite_uses_no_second_llm_audit():
    raw = "只有用户确认之后才能插入，否则保留结果供复制"
    candidate = "用户确认后才能插入，否则保留结果供复制"
    stub = StubLlm([response(candidate), audit("pass")])
    result = await RefinerService(stub, budget_ms=5_000).refine(raw)
    assert result.review_required is False
    assert result.text == candidate
    assert stub.calls == ["refine"]


def test_tc_asr_19_startup_rejects_missing_stable_partial(clean_database, monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main
    from tests.conftest import SERVER_ROOT, auth_headers, register_and_login, wait_startup_completion
    from tests.test_sessions import create_session

    class UnstableAsr:
        model_id = "unstable"
        capabilities = type(
            "Caps",
            (),
            {"stable_partial": False, "backend_version": "test"},
        )()

        async def prepare(self):
            return None

        async def health(self):
            return True

        async def close(self):
            return None

    monkeypatch.setattr(main, "build_asr", lambda config: UnstableAsr())
    fresh = main.create_app(str(SERVER_ROOT / "config" / "app.yaml"))
    with TestClient(fresh) as client:
        wait_startup_completion(client)
        health = client.get("/healthz")
        assert health.status_code == 503
        body = health.json()
        assert body["ready"] is False
        assert body["degraded"] is True
        # Public readiness uses a bounded reason code, not provider exception text.
        assert body["unavailable_reason"] == "ASR_UNAVAILABLE"
        assert fresh.state.asr_prepared is False
        tokens = register_and_login(client, email="asr19-not-ready@example.com")
        headers = auth_headers(tokens)
        capabilities = client.get("/api/v1/capabilities", headers=headers)
        assert capabilities.status_code == 200
        assert capabilities.json()["ready"] is False
        rejected = create_session(client, headers, "asr19-not-ready")
        assert rejected.status_code == 503
        assert rejected.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_tc_asr_19_partial_regression_fails_session(app_client):
    from starlette.websockets import WebSocketDisconnect

    from app.providers.asr.base import AsrPartial
    from tests.conftest import auth_headers, register_and_login
    from tests.test_sessions import create_session, dictation, frame

    class RegressingSession:
        def __init__(self):
            self.cancelled = False

        async def feed(self, pcm, start):
            return None

        async def partials(self):
            yield AsrPartial(1, "你好", "", 4000, 4000)
            yield AsrPartial(2, "世界", "", 8000, 8000)

        async def finish(self, total, deadline):
            raise AssertionError("finish must not be reached after regression")

        async def cancel(self):
            self.cancelled = True

    provider = app_client.app.state.sessions.asr_provider

    async def open_regressing(options):
        return RegressingSession()

    original = provider.open
    provider.open = open_regressing
    try:
        tokens = register_and_login(app_client)
        headers = auth_headers(tokens)
        session = create_session(app_client, headers, "asr19-regression").json()
        seen = []
        try:
            with app_client.websocket_connect(
                f"/ws/v1/sessions/{session['session_id']}/audio", headers=headers
            ) as ws:
                ws.receive_json()
                ws.send_bytes(frame(0))
                while True:
                    event = ws.receive_json()
                    seen.append(event)
                    if event["type"] == "error":
                        break
        except (WebSocketDisconnect, Exception):
            pass
        assert any(
            event.get("type") == "error"
            and event["error"]["code"] == "ASR_FAILED"
            for event in seen
        )
    finally:
        provider.open = original


def test_tc_asr_20_llm_unavailable_degrades_but_allows_asr(app_client, monkeypatch):
    from app.providers.asr.base import ProviderError
    from app.providers.llm.base import LlmResponse
    from tests.conftest import auth_headers, register_and_login
    from tests.test_sessions import create_session, dictation

    llm = app_client.app.state.sessions.refiner.llm

    async def unavailable(request, deadline):
        return LlmResponse(
            parsed=None,
            provider="mock",
            model_id="mock-llm",
            error=ProviderError("unavailable", "llm offline"),
        )

    monkeypatch.setattr(llm, "generate", unavailable)
    app_client.app.state.degraded = True
    app_client.app.state.llm_available = False
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    capabilities = app_client.get("/api/v1/capabilities", headers=headers).json()
    assert capabilities["ready"] is True
    assert capabilities["degraded"] is True
    assert capabilities["llm"]["available"] is False
    assert capabilities["asr"]["model_id"] == "mock-asr"
    session = create_session(app_client, headers, "asr20-session").json()
    events = dictation(app_client, headers, session["session_id"])
    result = events[-1]["result"]
    assert result["refined_text"] == result["raw_text"]
    assert result["review_required"] is True
    assert result["review_reason"] == "llm_unavailable"
