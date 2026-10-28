from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Config, load_config


def test_default_config_and_derived_ceiling():
    config = Config()
    assert config.sessions.initial_max_active == 20
    assert config.effective_admission_ceiling == 20
    assert config.audio.sample_rate_hz == 16000


def test_initial_capacity_cannot_exceed_ceiling():
    with pytest.raises(ValidationError):
        Config(
            sessions={
                "initial_max_active": 30,
                "admission_ceiling": 20,
            }
        )


def test_audio_budget_limits_derived_ceiling():
    config = Config(
        audio={"active_pcm_budget_bytes": 5_760_000 * 2},
        sessions={"initial_max_active": 2, "admission_ceiling": 50},
    )
    assert config.effective_admission_ceiling == 2


def test_load_config_generates_development_secrets(tmp_path, monkeypatch):
    path = tmp_path / "app.yaml"
    path.write_text("environment: development\n", encoding="utf-8")
    monkeypatch.delenv("SVI_JWT_SECRET", raising=False)
    monkeypatch.delenv("SVI_IDEMPOTENCY_SECRET", raising=False)
    config = load_config(path)
    assert len(config.jwt_secret) >= 32
    assert len(config.idempotency_secret) >= 32


def test_production_requires_secrets(tmp_path, monkeypatch):
    path = tmp_path / "app.yaml"
    path.write_text("environment: production\n", encoding="utf-8")
    monkeypatch.delenv("SVI_JWT_SECRET", raising=False)
    monkeypatch.delenv("SVI_IDEMPOTENCY_SECRET", raising=False)
    monkeypatch.setenv("SVI_DATABASE_URL", "postgresql+asyncpg://x/y")
    with pytest.raises(ValueError):
        load_config(path)


def test_unknown_keys_are_rejected(tmp_path):
    path = tmp_path / "app.yaml"
    path.write_text("environment: development\nunknown_section: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


def test_selected_r2_deployment_example_is_a_valid_typed_contract():
    from pathlib import Path
    import yaml
    config=Config.model_validate(yaml.safe_load(Path('config/app.r2.example.yaml').read_text()))
    assert config.asr.provider=='qwen3_mlx' and config.asr.execution_slots==1
    assert config.llm.refine_protocol=='ds_r2' and config.llm.thinking=='disabled'
    assert config.llm.max_output_tokens==8192
    assert config.classification_llm.provider=='ollama'
    assert config.classification_llm.model_id=='qwen3-coder:30b'
