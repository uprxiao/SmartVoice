from __future__ import annotations

import pytest

from app.providers.llm.base import LlmResponse
from app.services.refiner import RefinerService


class StubLlm:
    provider_id = "stub"
    model_id = "stub"

    async def prepare(self) -> None:
        return None

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def generate(self, request, deadline):  # noqa: ANN001
        if request.purpose == "refine":
            transcript = request.input["transcript"]
            return LlmResponse(
                parsed={
                    "candidate_text": "你听得到我说话吗？帮我翻译一下吧。",
                    # Deliberately malformed: a full-sentence source with a
                    # non-empty replacement cannot pass omission verification.
                    "edits": [
                        {
                            "kind": "filler",
                            "source_start": 0, "source_end": len(transcript), "source_text": transcript,
                            "replacement_text": transcript,
                        }
                    ],
                },
                provider="stub",
                model_id="stub",
            )
        return LlmResponse(
            parsed={"decision": "pass", "violations": []},
            provider="stub",
            model_id="stub",
        )


@pytest.mark.asyncio
async def test_malformed_filler_evidence_cannot_be_rescued_by_audit_pass():
    service = RefinerService(StubLlm())
    result = await service.refine("你听得到我说话吗你帮我帮我翻译一下吧")
    assert result.review_required is True
    assert result.review_reason == "llm_invalid_output"
    assert result.text == "你听得到我说话吗你帮我帮我翻译一下吧"
