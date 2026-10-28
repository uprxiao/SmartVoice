"""NFR10 config-only complements: no factory, model, HTTP, DB or application.

Explicit missing references are deployment errors. No reference remains valid
for providers that require no key. Secret values are private config attributes.
Prepared offline; root must compile/run before recording any result.
"""
from __future__ import annotations

import json
import secrets

import pytest

from app.core.config import load_config


ASR_KEY = "SVI_TEST_ASR_REFERENCE_BOUNDARY"
LLM_KEY = "SVI_TEST_LLM_REFERENCE_BOUNDARY"
OVERRIDES = (
    "SVI_HOST", "SVI_PORT", "SVI_ASR_PROVIDER", "SVI_ASR_MODEL_PATH",
    "SVI_LLM_PROVIDER", "SVI_LLM_BASE_URL", "SVI_LLM_MODEL_ID",
)


def config_file(tmp_path, monkeypatch, *, references):
    for name in OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SVI_ENVIRONMENT", "production")
    # Syntactically valid, test-only URL: this module only calls load_config.
    # No client/engine is constructed and no connection is attempted.
    monkeypatch.setenv("SVI_DATABASE_URL", "postgresql+asyncpg://fixture:synthetic_only@127.0.0.1:1/nfr10_config_only")
    values = {name: "NFR10-SYNTHETIC-" + secrets.token_hex(24) for name in
              ("SVI_JWT_SECRET", "SVI_IDEMPOTENCY_SECRET", ASR_KEY, LLM_KEY)}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    data = {"environment": "production", "asr": {"provider": "mock"},
            "llm": {"provider": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
                    "model_id": "config-only-fixture"}}
    if references == "explicit":
        data["asr"]["api_key_env"] = ASR_KEY
        data["llm"]["api_key_env"] = LLM_KEY
    elif references == "null":
        data["asr"]["api_key_env"] = data["llm"]["api_key_env"] = None
    else:
        assert references == "omitted"
    path = tmp_path / "config.yaml"
    # JSON is valid YAML; no secret value is present in this document.
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, values


def assert_secrets_absent(values, text):
    leaked = any(value in text for value in values.values())
    # Only the boolean is exposed if this assertion fails, never the values.
    assert not leaked, "synthetic secret value exposed by config output"


@pytest.mark.parametrize("environment_value", [None, ""], ids=["absent", "empty"])
def test_explicit_asr_key_reference_must_resolve_without_echoing_other_keys(
    tmp_path, monkeypatch, caplog, capsys, environment_value,
):
    path, values = config_file(tmp_path, monkeypatch, references="explicit")
    # A valid referenced LLM key is already present: the new ASR branch must be
    # reached and cannot pass by rejecting an unrelated LLM/JWT/DB prerequisite.
    if environment_value is None:
        monkeypatch.delenv(ASR_KEY)
    else:
        monkeypatch.setenv(ASR_KEY, environment_value)
    with pytest.raises(ValueError) as failure:
        load_config(path)
    output = capsys.readouterr()
    message = str(failure.value)
    component_is_identified = "asr" in message.casefold() and "key" in message.casefold()
    missing_is_identified = "missing" in message.casefold()
    assert component_is_identified and missing_is_identified, "missing ASR-key deployment detail"
    assert_secrets_absent(values, message + caplog.text + output.out + output.err)


@pytest.mark.parametrize("references", ["omitted", "null"])
def test_no_provider_key_reference_does_not_invent_a_required_secret(
    tmp_path, monkeypatch, caplog, capsys, references,
):
    path, values = config_file(tmp_path, monkeypatch, references=references)
    monkeypatch.delenv(ASR_KEY)
    monkeypatch.delenv(LLM_KEY)
    config = load_config(path)
    assert config.asr.api_key_env is None and config.llm.api_key_env is None
    assert config.asr_api_key is None and config.llm_api_key is None
    output = capsys.readouterr()
    assert_secrets_absent(values, repr(config) + config.model_dump_json() + caplog.text + output.out + output.err)


def test_present_provider_references_bind_exact_values_but_keep_them_private(
    tmp_path, monkeypatch, caplog, capsys,
):
    path, values = config_file(tmp_path, monkeypatch, references="explicit")
    config = load_config(path)
    bound_correctly = (config.asr_api_key == values[ASR_KEY] and config.llm_api_key == values[LLM_KEY]
        and config.jwt_secret == values["SVI_JWT_SECRET"]
        and config.idempotency_secret == values["SVI_IDEMPOTENCY_SECRET"])
    assert bound_correctly, "explicit config reference did not bind its original environment value"
    public = config.model_dump()
    assert public["asr"]["api_key_env"] == ASR_KEY and public["llm"]["api_key_env"] == LLM_KEY
    assert {"_asr_api_key", "_llm_api_key", "_jwt_secret", "_idempotency_secret"}.isdisjoint(public)
    output = capsys.readouterr()
    assert_secrets_absent(values, repr(config) + str(config) + json.dumps(public) +
                         config.model_dump_json() + caplog.text + output.out + output.err + path.read_text())
