"""Shared protected literals through the current DEFAULT Refiner pipeline.

Only the LLM provider's returned documents are scripted. This verifies local
control flow, never real-model semantics, ASR, HTTP adapters or Desktop E2E.
"""
from __future__ import annotations

import pytest

from app.providers.llm.base import LlmResponse
from app.services.refine_evidence import protected_problem
from app.services.refiner import RefinerService


class Answers:
    def __init__(self, candidate, edits):
        self.answers = [
            {"candidate_text": candidate, "edits": edits},
            {"decision": "pass", "violations": []},
        ]
        self.calls = []

    async def generate(self, request, deadline):
        self.calls.append((request, deadline))
        assert self.answers, "unexpected repair/retry request"
        return LlmResponse(parsed=self.answers.pop(0), provider="stub", model_id="url-guard-fixture")


def format_edit(raw, source, replacement):
    start = raw.index(source)
    return {"kind": "format", "source_start": start, "source_end": start + len(source),
        "source_text": source, "replacement_text": replacement}


BAD = [
    pytest.param("访问 example.com 查看说明。", "example.com", "example com", id="bare-host-dot-to-space"),
    pytest.param("访问 docs.example.org 查看说明。", "docs.example.org", "docs example.org", id="subdomain-dot-to-space"),
    pytest.param("访问 https://example.com 查看说明。", "example.com", "example com", id="scheme-host-dot-to-space"),
    pytest.param("访问 example.com:8443 查看说明。", "example.com:8443", "example.com 8443", id="bare-port-colon-to-space"),
    pytest.param("访问 https://example.com/path 查看说明。", "https:", "https ", id="scheme-colon-to-space"),
    pytest.param("访问 https://example.com/?mode=fast 查看说明。", "?", "", id="query-delimiter-delete"),
    pytest.param("等待3.5秒后重试。", "3.5", "35", id="decimal-regression"),
    pytest.param("读取 /var/log/app.log 后重试。", "app.log", "app log", id="path-regression"),
]


@pytest.mark.parametrize("raw,source,replacement", BAD)
def test_shared_guard_rejects_literal_punctuation_mutation(raw, source, replacement):
    candidate = raw.replace(source, replacement, 1)
    assert protected_problem(raw, candidate) is True


@pytest.mark.parametrize("raw,source,replacement", BAD)
@pytest.mark.asyncio
async def test_default_refiner_rejects_literal_format_before_permissive_audit(raw, source, replacement):
    candidate = raw.replace(source, replacement, 1)
    provider = Answers(candidate, [format_edit(raw, source, replacement)])
    # Defaults are intentional: no zero-retry shortcut, private alternate
    # Refiner, schema bypass, patched guard or shortened total budget.
    result = await RefinerService(provider).refine(raw)
    assert result.text == raw and result.review_required is True
    assert result.review_reason == "protected_content_changed"
    assert [request.purpose for request, _ in provider.calls] == ["refine"]
    assert len(provider.answers) == 1  # A model's audit=pass cannot mask the local failure.
    assert provider.calls[0][0].input["transcript"] == raw


GOOD = [
    pytest.param("访问 example.com 然后查看说明", "说明", "说明。", id="bare-host-preserved"),
    pytest.param("访问 https://example.com/a?mode=fast 然后重试", "重试", "重试。", id="full-url-preserved"),
    pytest.param("读取 /var/log/app.log 然后等待3.5秒", "秒", "秒。", id="path-decimal-preserved"),
    pytest.param("使用 TypeScript 然后检查 API", "API", "API。", id="english-case-preserved"),
    pytest.param("先检查权限 再开始录音", "权限 ", "权限。\n", id="t14-layout"),
    pytest.param("First. Then continue", "First. ", "First.\n", id="ordinary-english-sentence"),
    pytest.param("Visit example.com. Then continue", "continue", "continue.", id="bare-host-followed-by-period"),
]


@pytest.mark.parametrize("raw,source,replacement", GOOD)
def test_shared_guard_allows_layout_around_unchanged_literals(raw, source, replacement):
    candidate = raw.replace(source, replacement, 1)
    assert protected_problem(raw, candidate) is False


@pytest.mark.parametrize("raw,source,replacement", GOOD)
@pytest.mark.asyncio
async def test_default_refiner_keeps_normal_formatting_without_extra_audit(raw, source, replacement):
    candidate = raw.replace(source, replacement, 1)
    provider = Answers(candidate, [format_edit(raw, source, replacement)])
    result = await RefinerService(provider).refine(raw)
    assert result.text == candidate and result.review_required is False
    assert result.review_reason is None
    assert [request.purpose for request, _ in provider.calls] == ["refine"]


@pytest.mark.parametrize("raw,candidate", [
    ("使用 TypeScript。", "使用 typescript。"),
    ("不要修改配置。", "修改配置。"),
])
def test_existing_case_and_negation_guards_remain_strict(raw, candidate):
    assert protected_problem(raw, candidate) is True
