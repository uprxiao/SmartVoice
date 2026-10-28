"""NFR10 local negative deployment matrix, prepared offline.

Actual YAML/config/factory/lifespan/PG/readiness paths. HTTP egress and decoder
entry are tripwires, never success stubs; every negative case must reject before
reaching them. No real model, internet request or approved-intranet claim.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import secrets
import time

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app import main
from app.services.asr_core import DecodeWorker
from tests.conftest import TEST_URL, wait_for_readiness
from tests.cases.test_dict_cases import fetch_one, key


ABSENT_KEY_ENV = "SVI_NFR10_EXPLICITLY_MISSING_KEY"
PRESENT_KEY_ENV = "SVI_NFR10_PRESENT_TEST_KEY"
UPSTREAM = "https://nfr10-upstream.invalid/v1"
PUBLIC_ASR = "https://8.8.8.8/asr"
CASES = (
    "unknown_asr", "unknown_llm", "missing_asr_model", "wrong_asr_digest",
    "missing_referenced_llm_key", "public_asr_address",
)
OVERRIDES = (
    "SVI_ASR_PROVIDER", "SVI_ASR_MODEL_PATH", "SVI_LLM_PROVIDER",
    "SVI_LLM_BASE_URL", "SVI_LLM_MODEL_ID", "SVI_HOST", "SVI_PORT",
)


def configure(tmp_path, monkeypatch, case):
    for name in OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(ABSENT_KEY_ENV, raising=False)
    monkeypatch.setenv("SVI_DATABASE_URL", TEST_URL)
    monkeypatch.setenv("SVI_ENVIRONMENT", "development")
    monkeypatch.setenv("SVI_INSTANCE_LOCK", "1")
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    canaries = ["NFR10-SYNTHETIC-" + secrets.token_hex(24) for _ in range(3)]
    for name, value in zip(("SVI_JWT_SECRET", "SVI_IDEMPOTENCY_SECRET", PRESENT_KEY_ENV), canaries):
        monkeypatch.setenv(name, value)
    cache = tmp_path / "owned-model-cache"
    cache.mkdir()
    monkeypatch.setenv("HF_HOME", str(cache))
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(cache / "hub"))
    # Do not inject HF_HUB_OFFLINE: that would mask production enforcement.
    data = {"environment": "development", "asr": {"provider": "mock"},
            "llm": {"provider": "mock", "api_key_env": PRESENT_KEY_ENV}}
    if case == "unknown_asr":
        data["asr"]["provider"] = "nfr10_unsupported_asr"
    elif case == "unknown_llm":
        data["llm"]["provider"] = "nfr10_unsupported_llm"
    elif case == "missing_asr_model":
        data["asr"] = {"provider": "mlx_whisper", "model_id": "nfr10-asr-missing",
                       "model_path": str(tmp_path / "not-installed"), "model_sha256": "0" * 64}
        assert not Path(data["asr"]["model_path"]).exists()
    elif case == "wrong_asr_digest":
        folder = tmp_path / "owned-invalid-model"
        folder.mkdir()
        config_bytes, weight_bytes = b'{"fixture":"not a real model"}', b"NFR10-NOT-MODEL-WEIGHTS"
        (folder / "config.json").write_bytes(config_bytes)
        (folder / "weights.npz").write_bytes(weight_bytes)
        wrong = "0" * 64
        assert hashlib.sha256(config_bytes + weight_bytes).hexdigest() != wrong
        data["asr"] = {"provider": "mlx_whisper", "model_id": "nfr10-asr-bad-digest",
                       "model_path": str(folder), "model_sha256": wrong}
    elif case in ("missing_referenced_llm_key", "runtime_llm_unreachable"):
        data["llm"] = {"provider": "openai_compatible", "model_id": "nfr10-fixture-model",
                       "base_url": UPSTREAM, "structured_output": "json_object",
                       "api_key_env": ABSENT_KEY_ENV if case == "missing_referenced_llm_key" else PRESENT_KEY_ENV}
    elif case == "public_asr_address":
        # This contradictory local-adapter configuration must be rejected,
        # not silently accepted by ignoring the public remote_base_url field.
        # It does not pretend a remote ASR adapter exists or send it audio.
        data["asr"]["remote_base_url"] = PUBLIC_ASR
    else:
        raise AssertionError("unknown negative fixture")
    config_path = tmp_path / "app.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("SVI_CONFIG", str(config_path))
    return config_path, cache, canaries


def tripwires(monkeypatch):
    seen = {"http": [], "decoder": []}

    def record_http(request):
        # Metadata only: no Authorization/header values, body, URL credentials.
        seen["http"].append((request.method, request.url.host, request.url.path))

    async def no_async_http(_transport, request):
        record_http(request)
        raise httpx.ConnectError("NFR10_TEST_NETWORK_TRIPWIRE", request=request)

    def no_sync_http(_transport, request):
        record_http(request)
        raise httpx.ConnectError("NFR10_TEST_NETWORK_TRIPWIRE", request=request)

    async def no_decoder(_worker, *_args, **_kwargs):
        seen["decoder"].append("decode")
        raise RuntimeError("NFR10_TEST_DECODER_TRIPWIRE")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_async_http)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_sync_http)
    monkeypatch.setattr(DecodeWorker, "decode", no_decoder)
    return seen


def deployment_diagnostic(case, diagnostic):
    text = diagnostic.casefold()
    # No new exact API error code is invented. Require a recognizable missing
    # component/reason in the actual startup exception, readiness or logs.
    if case.startswith("unknown_"):
        component = "asr" if case == "unknown_asr" else "llm"
        return component in text and any(word in text for word in ("unsupported", "unknown", "未知", "未实现", "不支持"))
    if case == "missing_asr_model":
        return any(word in text for word in ("model", "模型")) and any(word in text for word in ("missing", "incomplete", "not found", "不存在", "不完整", "缺"))
    if case == "wrong_asr_digest":
        return any(word in text for word in ("digest", "sha256", "摘要")) and any(word in text for word in ("mismatch", "wrong", "不符", "错误", "校验"))
    if case == "missing_referenced_llm_key":
        return any(word in text for word in ("key", "secret", "密钥")) and any(word in text for word in ("missing", "unset", "not set", "缺", "未配置"))
    if case == "public_asr_address":
        return "asr" in text and any(word in text for word in ("remote", "public", "公网", "远端", "地址")) and any(word in text for word in ("forbid", "unsupported", "invalid", "must", "禁止", "不允许", "不支持", "未实现", "只", "拒绝"))
    return False


def no_business_rows():
    # The inherited fixture owns this unique PG database. Read only here.
    return fetch_one("""SELECT
        (SELECT count(*) FROM sessions) + (SELECT count(*) FROM results) +
        (SELECT count(*) FROM insertions) + (SELECT count(*) FROM learning_events) +
        (SELECT count(*) FROM dictionary_terms) + (SELECT count(*) FROM model_calls)
    """) == 0


def check_safe_and_offline(seen, cache, canaries, diagnostic, *, permit_transport_probe=False):
    leaked = any(value in diagnostic for value in canaries)
    assert not leaked, "synthetic deployment secret appeared in startup/log/health output"
    database_url_leaked = TEST_URL in diagnostic
    assert not database_url_leaked, "database URL appeared in startup/log/health output"
    assert seen["decoder"] == [], "bad deployment reached model loading/inference"
    assert list(cache.rglob("*")) == [], "startup populated the owned model cache"
    if not permit_transport_probe:
        assert seen["http"] == [], "invalid configuration reached HTTP instead of failing before egress"
    assert no_business_rows()


def settled_invalid_readiness(client):
    # Only STARTING may continue. Return unexpected/final responses to the
    # existing offline/secret checks before asserting the deployment verdict.
    deadline = time.monotonic() + 5
    while True:
        response = client.get("/health/ready")
        remaining = deadline - time.monotonic()
        if (response.status_code != 503 or response.json().get("reason_code") != "STARTING"
                or remaining <= 0):
            return response, remaining >= 0
        time.sleep(min(.01, remaining))


@pytest.mark.parametrize("case", CASES)
def test_tc_nfr_10_invalid_deployment_is_rejected_before_egress_or_model_load(
    clean_database, monkeypatch, tmp_path, caplog, case,
):
    config_path, cache, canaries = configure(tmp_path, monkeypatch, case)
    seen = tripwires(monkeypatch)
    caplog.set_level(logging.INFO)
    entered, startup_error, responses = False, None, {}
    settled_in_time = False
    app = None
    try:
        app = main.create_app(str(config_path))
        with TestClient(app) as client:
            entered = True
            responses["live"] = client.get("/health/live")
            responses["ready"], settled_in_time = settled_invalid_readiness(client)
            # Readiness gate must reject new work; no fake token or seeded user.
            request_id = key()
            responses["create"] = client.post("/api/v1/sessions",
                headers={"Idempotency-Key": request_id},
                json={"client_request_id": request_id, "learning_enabled": True})
    except Exception as exc:
        if entered:
            raise  # HTTP/assertion/teardown failures cannot count as startup refusal.
        startup_error = str(exc)
    diagnostic = (startup_error or "") + "\n" + caplog.text + "\n" + "\n".join(response.text for response in responses.values())
    # Check tripwires even if the app returned 503: a test-injected network/model
    # failure must never manufacture a passing invalid-config result.
    check_safe_and_offline(seen, cache, canaries, diagnostic)
    if startup_error is None:
        assert entered
        assert settled_in_time, "invalid deployment remained STARTING past the fixture deadline"
        assert responses["live"].status_code == 200
        assert responses["ready"].status_code == 503, {"case": case, "ready_status": responses["ready"].status_code}
        assert responses["ready"].json()["status"] == "not_ready"
        expected_reason = {
            "unknown_asr": "ASR_UNAVAILABLE", "unknown_llm": "STARTUP_DEPENDENCY_UNAVAILABLE",
            "missing_asr_model": "ASR_UNAVAILABLE", "wrong_asr_digest": "ASR_UNAVAILABLE",
            "public_asr_address": "ASR_UNAVAILABLE",
        }
        assert case in expected_reason, "explicit missing key must fail config construction"
        assert responses["ready"].json().get("reason_code") == expected_reason[case]
        assert responses["create"].status_code == 503
        error = responses["create"].json()["error"]
        assert error["code"] == "SERVICE_UNAVAILABLE" and error["retryable"] is True and error["request_id"]
    assert deployment_diagnostic(case, diagnostic), {"case": case, "missing_observable_detail": True}


def test_tc_nfr_10_valid_configuration_runtime_llm_unreachable_remains_degraded(
    clean_database, monkeypatch, tmp_path, caplog,
):
    config_path, cache, canaries = configure(tmp_path, monkeypatch, "runtime_llm_unreachable")
    seen = tripwires(monkeypatch)
    caplog.set_level(logging.INFO)
    with TestClient(main.create_app(str(config_path))) as client:
        wait_for_readiness(client)
        response = client.get("/health/ready")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "degraded"
        assert response.json()["dependencies"] == {"database": "ok", "asr": "ready", "llm": "unavailable"}
    diagnostic = response.text + "\n" + caplog.text
    check_safe_and_offline(seen, cache, canaries, diagnostic, permit_transport_probe=True)
    assert seen["http"]
    assert all(method == "GET" and host == "nfr10-upstream.invalid" and path == "/v1/models"
               for method, host, path in seen["http"])
