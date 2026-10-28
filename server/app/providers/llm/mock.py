from __future__ import annotations

import time
from typing import Any

from .base import LlmProvider, LlmRequest, LlmResponse
from ..asr.base import ProviderError


EXACT_REFINE_SCHEMA_ID = "refine-v5-dictionary-no-audit"


class MockLlmProvider(LlmProvider):
    model_id = "mock-llm"
    provider_id = "mock"

    def __init__(self, refine_text: str | None = None):
        self.refine_text = refine_text

    async def prepare(self) -> None:
        return None

    async def generate(self, request: LlmRequest, deadline: float) -> LlmResponse:
        started = time.monotonic()
        if request.purpose == "refine":
            raw = request.input.get("transcript", "")
            # Deterministic fixture provider: only explicit leading filler has
            # verified local evidence. This is not semantic acceptance.
            prefix = next((p for p in ("嗯那个，", "嗯，") if raw.startswith(p)), "")
            cleaned = raw[len(prefix):]
            if request.output_mode == "text":
                return LlmResponse(None, self.provider_id, self.model_id,
                    text=self.refine_text if self.refine_text is not None else cleaned,
                    elapsed_ms=int((time.monotonic() - started) * 1000))
            if request.output_schema_id == EXACT_REFINE_SCHEMA_ID:
                # Model-wire fixture only. Quote the original transcript, never
                # manufacture numeric positions or bypass the real converter.
                # A repeated prefix needs an unambiguous original wrapper.
                before = prefix if prefix and raw.count(prefix) == 1 else raw
                after = "" if before == prefix else cleaned
                parsed: dict[str, Any] = {"edits": [
                    {"kind": "filler", "before": before, "after": after},
                ] if prefix else []}
                if self.refine_text is not None:
                    parsed["candidate_text"] = self.refine_text
            elif request.output_schema_id == "refine-v2-terms":
                parsed = {
                    "candidate_text": self.refine_text if self.refine_text is not None else cleaned,
                    "edits": [{"kind": "filler", "source_start": 0, "source_end": len(prefix),
                               "source_text": prefix, "replacement_text": ""}] if prefix else [],
                }
            else:
                return LlmResponse(parsed=None, provider=self.provider_id, model_id=self.model_id,
                    finish_reason="error", error=ProviderError("invalid_input", "unsupported Mock refine schema"))
        elif request.purpose == "audit":
            parsed = {"decision": "pass", "violations": []}
        elif request.purpose == "classify":
            before, after = str(request.input.get("before", "")), str(request.input.get("after", ""))
            parsed = {
                "decision": "suggest", "ambiguous": True, "category": "proper_term", "reason_code": "MOCK_SUGGESTION",
                "evidence": {"before_start": 0, "before_end": len(before), "before": before, "after": after},
            }
        else:
            parsed = {}
        return LlmResponse(
            parsed=parsed,
            provider="mock",
            model_id=self.model_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None
