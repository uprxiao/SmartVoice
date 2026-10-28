"""Mock wire compatibility only; does not demonstrate model semantics."""
from copy import deepcopy

import pytest

from app.core.clock import loop_time
from app.providers.llm.base import LlmRequest
from app.providers.llm.mock import MockLlmProvider
from app.services.llm_validation import matches_schema
from app.services.refine_edits_only import EDITS_ONLY_SCHEMA, EDITS_ONLY_VERSION
from app.services.refiner import REFINE_SCHEMA, RefinerService


@pytest.mark.asyncio
@pytest.mark.parametrize("exact", [False, True], ids=["numeric-legacy", "exact-production"])
@pytest.mark.parametrize("raw", ["保留配置。", "嗯，保留配置。", "嗯，标签原样写“嗯，”，保留配置。"])
async def test_mock_uses_selected_wire_and_real_refiner_guards(exact, raw):
    provider = MockLlmProvider()
    schema = EDITS_ONLY_SCHEMA if exact else REFINE_SCHEMA
    schema_id = EDITS_ONLY_VERSION if exact else "refine-v2-terms"
    request = LlmRequest("refine", "test-only", {"transcript": raw, "terms": []},
        deepcopy(schema), output_schema_id=schema_id, system_prompt_version=schema_id)
    response = await provider.generate(request, loop_time() + 10)
    assert response.error is None and matches_schema(response.parsed, schema)
    assert response.prompt_tokens is response.completion_tokens is None
    assert request.schema == schema
    if exact:
        assert set(response.parsed) == {"edits"}
        assert all(set(edit) == {"kind", "before", "after"} for edit in response.parsed["edits"])
    else:
        assert set(response.parsed) == {"candidate_text", "edits"}
        assert all("source_start" in edit and "before" not in edit for edit in response.parsed["edits"])
    result = await RefinerService(provider, edits_only_candidate=exact).refine(raw)
    assert result.review_required is False
    assert result.text == (raw[2:] if raw.startswith("嗯，") else raw)
    if "“嗯，”" in raw:
        assert "“嗯，”" in result.text


@pytest.mark.asyncio
async def test_mock_override_cannot_bypass_negation_guard_in_selected_exact_path():
    raw = "嗯，不要删除数据库。"
    service = RefinerService(MockLlmProvider(refine_text="删除数据库。"), edits_only_candidate=True)
    result = await service.refine(raw)
    assert result.text == raw and result.review_required is True
    assert result.review_reason == "protected_content_changed"


@pytest.mark.asyncio
async def test_mock_unknown_refine_schema_is_not_silently_numeric():
    response = await MockLlmProvider().generate(
        LlmRequest("refine", "test-only", {"transcript": "保留配置。"}, {},
            output_schema_id="unknown-protocol"), loop_time() + 10)
    assert response.parsed is None and response.error is not None
    assert response.error.code == "invalid_input"
