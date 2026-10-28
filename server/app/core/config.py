from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

PCM_BYTES_PER_SECOND = 32_000
MAX_SESSION_SECONDS = 180
SESSION_PCM_RESERVE = PCM_BYTES_PER_SECOND * MAX_SESSION_SECONDS


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(Strict):
    host: str = "127.0.0.1"
    port: int = Field(17863, ge=1, le=65535)
    workers: Literal[1] = 1
    instance_lock_key: int = 4_200_17863


class DatabaseConfig(Strict):
    url_env: str = "SVI_DATABASE_URL"
    pool_size: int = Field(5, ge=1, le=100)
    max_overflow: int = Field(5, ge=0, le=100)
    pool_timeout_seconds: float = Field(2.0, gt=0, allow_inf_nan=False)


class AuthConfig(Strict):
    jwt_secret_env: str = "SVI_JWT_SECRET"
    issuer: str = "smartvoiceinput-dev"
    audience: str = "smartvoiceinput"
    access_ttl_seconds: Literal[900] = 900
    refresh_ttl_seconds: Literal[2_592_000] = 2_592_000
    argon2_memory_kib: Literal[65_536] = 65_536
    argon2_time_cost: Literal[3] = 3
    argon2_parallelism: Literal[1] = 1
    max_parallel_hashes: int = Field(2, ge=1)


class SecurityConfig(Strict):
    idempotency_secret_env: str = "SVI_IDEMPOTENCY_SECRET"
    cors_origins: list[str] = Field(default_factory=list)
    ws_origins: list[str] = Field(default_factory=list)
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    asr_allowed_hosts: list[str] = Field(default_factory=list)
    # V2 §6.3: deny-public (V1 default) or allowlist; the list is deployment
    # configuration only (no admin entry) and takes effect on restart.
    egress_policy: Literal["deny-public", "allowlist"] = "deny-public"
    egress_allowlist: list[str] = Field(default_factory=list)


class AudioConfig(Strict):
    encoding: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate_hz: Literal[16000] = 16000
    channels: Literal[1] = 1
    chunk_ms: Literal[250] = 250
    # Deployment ceiling for one recording; PCM is reserved per session at this
    # length. A session's limit is min(this, current ASR provider's limit).
    max_duration_seconds: int = Field(180, ge=10, le=360)
    active_pcm_budget_bytes: int = Field(134_217_728, ge=5_760_000)


class SessionsConfig(Strict):
    initial_max_active: int = Field(20, ge=1, le=2_147_483_647)
    admission_ceiling: int = Field(20, ge=1, le=2_147_483_647)
    per_user: Literal[1] = 1
    queue_size: Literal[0] = 0
    connect_timeout_ms: int = Field(10_000, ge=1, le=10000)
    audio_idle_timeout_ms: int = Field(10_000, ge=1, le=10000)
    unacked_limit_ms: int = Field(2_000, ge=1, le=2000)
    auth_poll_ms: int = Field(5_000, ge=1, le=5000)


class RetryConfig(Strict):
    audio_ttl_seconds: int = Field(600, ge=1, le=600)
    max_cached_sessions: int = Field(20, ge=1, le=20)
    audio_budget_bytes: int = Field(134_217_728, ge=5_760_000)


class CleanupConfig(Strict):
    history_interval_seconds: int = Field(3_600, ge=1, le=3600)
    audio_interval_seconds: int = Field(1, ge=1, le=1)


class StatisticsConfig(Strict):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"


class LoggingConfig(Strict):
    retention_days: int = Field(14, ge=1)
    level: str = "INFO"


class AsrConfig(Strict):
    provider: str = "mock"
    model_id: str = "mock-asr"
    model_path: str = ""
    model_sha256: str = ""
    language: str = "auto"
    stable_partial_required: bool = True
    decode_window_ms: int = Field(10_000, ge=1_000)
    decode_interval_ms: int = Field(1_000, ge=250)
    # Local providers ≤10 s; up to 25 s only for cloud providers (V2 §4.4).
    finish_timeout_ms: int = Field(10_000, ge=1_000, le=25_000)
    params: dict = Field(default_factory=dict)
    execution_slots: int = Field(1, ge=1)
    decode_timeout_ms: int = Field(3_000, ge=500, le=3000)
    remote_base_url: str | None = None
    api_key_env: str | None = None
    runtime_path: str | None = None
    model_manifest_path: str | None = None


class LlmConfig(Strict):
    provider: str = "mock"
    model_id: str = "mock-llm"
    base_url: str = ""
    api_key_env: str | None = None
    structured_output: Literal["json_schema", "json_object", "prompt_json"] = "json_object"
    refine_protocol: Literal["ds_r2", "legacy_edits"] = "ds_r2"
    thinking: Literal["disabled"] | None = None
    temperature: Literal[0] = 0
    max_output_tokens: int = Field(8_192, ge=256)
    call_timeout_ms: int = Field(10_000, ge=1_000, le=10000)
    automatic_retries_per_attempt: int = Field(1, ge=0, le=1)
    fidelity_audit: Literal["off"] = "off"
    max_inflight: int = Field(4, ge=1)
    classify_queue_size: int = Field(20, ge=1, le=20)
    foreground_wait_ms: int = Field(2_000, ge=0, le=2000)
    classify_wait_ms: int = Field(1_000, ge=0, le=1000)


class PipelineConfig(Strict):
    stop_to_result_budget_ms: int = Field(30_000, ge=1_000, le=30000)
    classification_budget_ms: int = Field(10_000, ge=1_000, le=10000)


class CapacityConfig(Strict):
    validation_report_path: str | None = None


class Config(Strict):
    environment: Literal["development", "production"] = "development"
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    capacity: CapacityConfig = Field(default_factory=CapacityConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    statistics: StatisticsConfig = Field(default_factory=StatisticsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    classification_llm: LlmConfig | None = None
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)

    _database_url: str = PrivateAttr(default="")
    _jwt_secret: str = PrivateAttr(default="")
    _idempotency_secret: str = PrivateAttr(default="")
    _llm_api_key: str | None = PrivateAttr(default=None)
    _asr_api_key: str | None = PrivateAttr(default=None)
    _classification_llm_api_key: str | None = PrivateAttr(default=None)

    @property
    def database_url(self) -> str:
        return self._database_url

    @property
    def jwt_secret(self) -> str:
        return self._jwt_secret

    @property
    def idempotency_secret(self) -> str:
        return self._idempotency_secret

    @property
    def llm_api_key(self) -> str | None:
        return self._llm_api_key

    @property
    def asr_api_key(self) -> str | None:
        return self._asr_api_key

    @property
    def classification_llm_api_key(self) -> str | None:
        return self._classification_llm_api_key

    @field_validator("environment", mode="before")
    @classmethod
    def _env(cls, v):
        return v or "development"

    @model_validator(mode="after")
    def _validate_capacity(self):
        budget_slots = self.audio.active_pcm_budget_bytes // self.session_pcm_reserve
        if budget_slots < 1:
            raise ValueError("active_pcm_budget_bytes must allow at least one session")
        if self.sessions.initial_max_active > self.sessions.admission_ceiling:
            raise ValueError("initial_max_active cannot exceed admission_ceiling")
        if self.sessions.initial_max_active > budget_slots:
            raise ValueError("initial_max_active exceeds audio budget derived ceiling")
        return self

    @model_validator(mode="after")
    def _validate_classification_route(self):
        from .endpoints import is_local_endpoint
        from urllib.parse import urlsplit
        if (self.llm.provider == 'openai_compatible' and self.llm.refine_protocol == 'ds_r2'
                and urlsplit(self.llm.base_url).hostname == 'api.deepseek.com'
                and (self.llm.thinking != 'disabled' or self.llm.model_id != 'deepseek-flash')):
            raise ValueError('official DS-R2 requires deepseek-flash with thinking disabled')
        c = self.classification_llm
        if c is not None and (c.provider not in {"mock", "ollama", "openai_compatible"}
                or (c.provider != "mock" and not is_local_endpoint(c.base_url or
                    ("http://127.0.0.1:11434" if c.provider == "ollama" else "")))):
            raise ValueError("classification_llm must use an explicit local/private endpoint")
        return self

    @model_validator(mode="after")
    def _validate_asr_params(self):
        from ..providers.params import ASR_PARAMS
        from ..providers.params import CLOUD_ASR_TYPES
        if self.asr.finish_timeout_ms > 10_000 and self.asr.provider not in CLOUD_ASR_TYPES:
            raise ValueError("asr.finish_timeout_ms above 10000 is only allowed for cloud ASR")
        model = ASR_PARAMS.get(self.asr.provider)
        if model is not None:
            model.model_validate(self.asr.params)
        elif self.asr.params:
            raise ValueError("asr.params is not supported for this provider")
        return self

    @property
    def session_pcm_reserve(self) -> int:
        return PCM_BYTES_PER_SECOND * self.audio.max_duration_seconds

    @property
    def effective_admission_ceiling(self) -> int:
        return min(
            self.sessions.admission_ceiling,
            self.audio.active_pcm_budget_bytes // self.session_pcm_reserve,
        )


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def load_config(path: str | Path | None = None) -> Config:
    """Load YAML then overlay declared environment variables.

    Unknown YAML keys are rejected by Strict models. Secrets are read from
    environment variables only; development may synthesize temporary ones.
    """
    cfg_path = Path(path) if path else Path(os.environ.get("SVI_CONFIG", "config/app.yaml"))
    data = _read_yaml(cfg_path)

    # Declared environment overrides.
    if v := _env("SVI_ENVIRONMENT"):
        data["environment"] = v
    if v := _env("SVI_HOST"):
        data.setdefault("server", {})["host"] = v
    if v := _env("SVI_PORT"):
        data.setdefault("server", {})["port"] = int(v)
    if v := _env("SVI_ASR_PROVIDER"):
        data.setdefault("asr", {})["provider"] = v
    if v := _env("SVI_ASR_MODEL_PATH"):
        data.setdefault("asr", {})["model_path"] = v
    if v := _env("SVI_LLM_PROVIDER"):
        data.setdefault("llm", {})["provider"] = v
    if v := _env("SVI_LLM_BASE_URL"):
        data.setdefault("llm", {})["base_url"] = v
    if v := _env("SVI_LLM_MODEL_ID"):
        data.setdefault("llm", {})["model_id"] = v

    config = Config.model_validate(data)

    database_url = _env(config.database.url_env)
    if not database_url:
        if config.environment == "production":
            raise ValueError(f"missing database URL environment {config.database.url_env}")
        database_url = "postgresql+asyncpg://localhost/smartvoiceinput"
    config._database_url = database_url

    jwt_secret = _env(config.auth.jwt_secret_env)
    if not jwt_secret:
        if config.environment == "production":
            raise ValueError(f"missing JWT secret environment {config.auth.jwt_secret_env}")
        jwt_secret = secrets.token_urlsafe(48)
    if config.environment == "production" and len(jwt_secret.encode()) < 32:
        raise ValueError("JWT secret must have at least 32 bytes")
    config._jwt_secret = jwt_secret

    idem_secret = _env(config.security.idempotency_secret_env)
    if not idem_secret:
        if config.environment == "production":
            raise ValueError(
                f"missing idempotency secret environment {config.security.idempotency_secret_env}"
            )
        idem_secret = secrets.token_urlsafe(48)
    if config.environment == "production" and len(idem_secret.encode()) < 32:
        raise ValueError("idempotency secret must have at least 32 bytes")
    config._idempotency_secret = idem_secret

    config._llm_api_key = _env(config.llm.api_key_env) if config.llm.api_key_env else None
    config._asr_api_key = _env(config.asr.api_key_env) if config.asr.api_key_env else None
    c = config.classification_llm
    config._classification_llm_api_key = _env(c.api_key_env) if c and c.api_key_env else None
    if config.llm.api_key_env and config._llm_api_key is None:
        raise ValueError("missing LLM API key environment reference")
    if config.asr.api_key_env and config._asr_api_key is None:
        raise ValueError("missing ASR API key environment reference")
    if c and c.api_key_env and config._classification_llm_api_key is None:
        raise ValueError("missing classification LLM API key environment reference")

    return config
