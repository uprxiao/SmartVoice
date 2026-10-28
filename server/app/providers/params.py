"""Per-type provider parameter allowlists (V2 design §6.2).

Shared by the YAML fallback (``asr.params``) and admin-managed provider rows:
unknown fields and out-of-range values are rejected (HTTP 422 in the admin
API). LLM refine protocol, temperature etc. stay fixed V1 values.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class BailianRealtimeParams(_Strict):
    language: str = Field('auto', min_length=2, max_length=16)
    segment_target_s: float = Field(30, ge=15, le=60)
    segment_window_s: float = Field(5, ge=1, le=10)
    segment_silence_ratio: float = Field(0.15, gt=0, le=1)
    segment_min_s: float = Field(10, ge=5, le=60)
    catchup_rate: float = Field(2.0, gt=0, le=2.0)
    max_reconnects: int = Field(8, ge=0, le=20)
    max_record_seconds: int = Field(360, ge=10, le=360)
    max_concurrent_sessions: int = Field(8, ge=1, le=200)
    use_env_proxy: bool = False
    connect_timeout_ms: int = Field(15_000, ge=1_000, le=30_000)


class LocalAsrParams(_Strict):
    max_record_seconds: int = Field(180, ge=10, le=180)


class LlmParams(_Strict):
    max_inflight: int = Field(4, ge=1, le=64)


ASR_PARAMS: dict[str, type[_Strict]] = {
    'bailian_realtime': BailianRealtimeParams,
    'qwen3_mlx': LocalAsrParams,
    'mock': LocalAsrParams,
}
LLM_PARAMS: dict[str, type[_Strict]] = {
    'openai_compatible': LlmParams,
    'ollama': LlmParams,
    'mock': LlmParams,
}
CLOUD_ASR_TYPES = frozenset({'bailian_realtime'})
ProviderKind = Literal['asr', 'llm']


def params_model(kind: str, provider_type: str) -> type[_Strict]:
    table = ASR_PARAMS if kind == 'asr' else LLM_PARAMS
    try:
        return table[provider_type]
    except KeyError:
        raise ValueError(f'unsupported {kind} provider type') from None
