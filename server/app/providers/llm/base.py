from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..asr.base import ProviderError


@dataclass
class LlmRequest:
    purpose: str  # refine | audit | classify
    system_prompt: str
    input: dict[str, Any]
    schema: dict[str, Any]
    max_output_tokens: int = 4_096
    call_id: str = ""
    system_prompt_version: str = ""
    output_schema_id: str = ""
    temperature: float = 0.0
    output_mode: Literal["json", "text"] = "json"


@dataclass
class LlmResponse:
    parsed: dict[str, Any] | None
    provider: str
    model_id: str
    finish_reason: str = "stop"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    elapsed_ms: int = 0
    error: ProviderError | None = None
    text: str | None = None


class LlmProvider(Protocol):
    model_id: str
    provider_id: str

    async def prepare(self) -> None: ...
    async def generate(self, request: LlmRequest, deadline: float) -> LlmResponse: ...
    async def health(self) -> bool: ...
    async def close(self) -> None: ...
