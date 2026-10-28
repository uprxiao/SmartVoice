"""Deterministic candidate-effect controls; not real model cleanup acceptance.

Only current numeric wire is used here. The frozen optional-protocol known-gap
test remains unchanged and needs its separately supplied experimental hook.
"""
import asyncio
from copy import deepcopy

import pytest

from app.providers.llm.base import LlmResponse
from app.services.refiner import RefinerService


class Answers:
    def __init__(self, candidate, proof):
        self.responses = [{'candidate_text': candidate, 'edits': [proof]},
                          {'decision': 'pass', 'violations': []}]
        self.calls = []

    async def generate(self, request, deadline):
        self.calls.append((request, deadline))
        assert self.responses, 'guard must not introduce a third request or semantic retry'
        return LlmResponse(parsed=deepcopy(self.responses.pop(0)), provider='stub', model_id='filler-effect-control')


def run(raw, candidate, source, *, start=None):
    if start is None:
        start = raw.index(source)
    proof = {'kind': 'filler', 'source_start': start, 'source_end': start + len(source),
             'source_text': source, 'replacement_text': ''}
    provider = Answers(candidate, proof)
    result = asyncio.run(RefinerService(provider).refine(raw))
    return result, provider


def audited(provider, candidate):
    # Historical helper name retained; the new contract forbids a second call.
    assert [request.purpose for request, _ in provider.calls] == ['refine']
    assert provider.calls[0][0].input['transcript']


def test_declared_filler_must_not_be_accepted_when_only_other_words_changed():
    # Same raw/candidate, call-order and desired oracle as the archived known
    # gap. Only wire spelling is current production's canonical numeric schema.
    raw = '嗯那个，帮我加一个按钮，然后然后记得不要改数据库'
    candidate = '嗯那个，帮我加一个按钮，然后记得不要改数据库'
    result, provider = run(raw, candidate, '嗯那个')
    audited(provider, candidate)
    assert result.review_required is True and result.text == raw
    assert result.review_reason == 'llm_invalid_output'


def test_actual_filler_deletion_keeps_natural_word_order_rewrite():
    raw = '嗯，今天我们先整理文档再开会。'
    candidate = '今天先整理文档，然后我们再开会。'
    result, provider = run(raw, candidate, '嗯，')
    audited(provider, candidate)
    # It is deliberately NOT just raw with the filler removed.
    assert result.text == candidate and not result.review_required


def test_deleted_spoken_filler_does_not_match_same_literal_inside_quote():
    raw = '🙂嗯，先记录“嗯”，再开始说明。'
    candidate = '🙂先记录“嗯”，随后开始说明。'
    result, provider = run(raw, candidate, '嗯，')
    audited(provider, candidate)
    assert result.text == candidate and not result.review_required


@pytest.mark.parametrize('wrong_occurrence', [False, True], ids=['declared_deleted', 'other_deleted'])
def test_same_filler_at_two_distinct_contexts_keeps_position_identity(wrong_occurrence):
    raw = '开场请先确认页面嗯，然后继续记录当前步骤。结束请先确认项目嗯，然后执行全部校验。'
    start = raw.rindex('嗯')
    removed = raw.index('嗯') if wrong_occurrence else start
    candidate = raw[:removed] + raw[removed + 1:]
    result, provider = run(raw, candidate, '嗯', start=start)
    audited(provider, candidate)
    assert result.review_required is wrong_occurrence
    assert result.text == (raw if wrong_occurrence else candidate)
    assert result.review_reason == ('llm_invalid_output' if wrong_occurrence else None)


def test_repeated_adjacent_context_is_not_arbitrarily_assigned_to_remaining_occurrence():
    phrase = '请先阅读操作说明嗯，然后保存当前记录。'
    raw = '开始。' + phrase + phrase + '收尾。'
    start = raw.index('嗯')
    candidate = raw[:start] + raw[start + 1:]
    result, provider = run(raw, candidate, '嗯', start=start)
    audited(provider, candidate)
    # Only the declared first occurrence was removed. The identical window
    # around the other occurrence cannot be assigned to it by a diff heuristic.
    # Audit is the semantic oracle here, not a claim of complete real cleanup.
    assert result.text == candidate and not result.review_required


def test_edit_inside_original_quote_remains_invalid_before_audit():
    raw = '标签内容是“嗯”，再开始说明。'
    candidate = '标签内容是“”，随后开始说明。'
    result, provider = run(raw, candidate, '嗯')
    assert [request.purpose for request, _ in provider.calls] == ['refine']
    assert result.text == raw and result.review_required
    assert result.review_reason == 'llm_invalid_output'
