"""Candidate adapter controls only; stub audit is not real semantic evidence."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.providers.llm.base import LlmResponse
from app.services.refine_edits_only import EDITS_ONLY_SCHEMA, EDITS_ONLY_VERSION, resolve_edits_only
from app.services.refiner import RefinerService
from app.services.refine_evidence import validate_edits

PASS = {'decision': 'pass', 'violations': []}
TERM = {'id': '00000000-0000-0000-0000-000000000001', 'term': 'Claude',
        'aliases': ['cloud'], 'ambiguous_alias_norms': [], 'contexts': []}


class Answers:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def generate(self, request, deadline):
        self.calls.append((request, deadline))
        assert self.responses, 'unexpected extra provider invocation'
        return LlmResponse(parsed=deepcopy(self.responses.pop(0)), provider='stub', model_id='optional-candidate-fixture')


def run(raw, wire, *, terms=None, audit=PASS, retries=0):
    provider = Answers(wire, audit)
    result = asyncio.run(RefinerService(provider, automatic_retries=retries,
                                      edits_only_candidate=True).refine(raw, terms))
    return result, provider


def canonical_evidence(raw, wire, terms=None):
    candidate, proofs = resolve_edits_only(raw, wire, terms or [])
    _, corrections, term_edits = validate_edits(raw, candidate, proofs, terms or [])
    return {'raw':raw,'candidate':candidate,'verified_self_corrections':corrections,
            'verified_term_corrections':term_edits}


def term_proof():
    return {'kind': 'term_correction', 'before': '用cloud助手', 'after': '用Claude助手',
            'dictionary_term_id': TERM['id'], 'dictionary_alias': 'cloud'}


@pytest.mark.parametrize('raw,candidate,proofs', [
    ('只有用户确认之后才能插入，否则保留结果供复制',
     '用户确认后才能插入，否则保留结果供复制', []),
    ('嗯，日志先保存，再检查配置', '先保存日志，再检查配置',
     [{'kind': 'filler', 'before': '嗯，', 'after': ''}]),
    ('日志先保存日志先保存，再检查配置', '先保存日志，再检查配置',
     [{'kind': 'repetition', 'before': '日志先保存日志先保存', 'after': '日志先保存'}]),
])
def test_complete_natural_rewrite_uses_candidate_guard_and_real_audit_input(raw, candidate, proofs):
    wire = {'edits': proofs, 'candidate_text': candidate}
    selected, canonical = resolve_edits_only(raw, wire, [])
    assert selected == candidate
    assert len(canonical) == len(proofs)
    for proof in canonical:
        assert raw[proof['source_start']:proof['source_end']] == proof['source_text']
    result, provider = run(raw, wire)
    assert result.text == candidate and result.review_required is False
    assert [request.purpose for request, _ in provider.calls] == ['refine']
    assert provider.calls[0][0].input['transcript'] == raw
    assert provider.calls[0][0].schema == EDITS_ONLY_SCHEMA
    assert provider.calls[0][0].output_schema_id == EDITS_ONLY_VERSION
    assert len({deadline for _, deadline in provider.calls}) == 1


def test_candidate_absence_retains_derived_text_without_changing_canonical_proof():
    raw = '嗯，日志先保存，再检查配置'
    wire = {'edits': [{'kind': 'filler', 'before': '嗯，', 'after': ''}]}
    derived, original_proof = resolve_edits_only(raw, wire, [])
    selected, supplied_proof = resolve_edits_only(raw, {**wire, 'candidate_text': '先保存日志，再检查配置'}, [])
    assert derived == '日志先保存，再检查配置'
    assert selected == '先保存日志，再检查配置'
    assert supplied_proof == original_proof
    result, provider = run(raw, wire)
    assert result.text == derived and not result.review_required
    assert len(provider.calls) == 1


@pytest.mark.parametrize('candidate', [None, '', True, 1, [], {}, '甲' * 32769])
def test_invalid_supplied_candidate_never_falls_back_to_derived(candidate):
    raw = '嗯，检查日志。'
    wire = {'edits': [{'kind': 'filler', 'before': '嗯，', 'after': ''}], 'candidate_text': candidate}
    with pytest.raises(ValueError, match='EDITS_ONLY_SHAPE_INVALID'):
        resolve_edits_only(raw, wire, [])
    result, provider = run(raw, wire)
    assert result.text == raw and result.review_reason == 'llm_invalid_output'
    assert len(provider.calls) == 1


def test_term_after_natural_reorder_checks_actual_target_not_derived_delta():
    raw = '请先把日志保存，再用cloud助手，cloud云服务。'
    candidate = '先保存日志，再用Claude助手，cloud云服务。'
    wire = {'edits': [term_proof()], 'candidate_text': candidate}
    result, provider = run(raw, wire, terms=[TERM])
    assert result.text == candidate and not result.review_required
    assert [request.purpose for request, _ in provider.calls] == ['refine']
    audit_input = canonical_evidence(raw, wire, [TERM] if 'cloud' in raw else [])
    assert audit_input['candidate'] == candidate
    proof = audit_input['verified_term_corrections'][0]
    assert (proof['source_start'], proof['source_end']) == (raw.index('cloud'), raw.index('cloud') + 5)
    assert proof['dictionary_term_id'] == TERM['id']
    assert proof['dictionary_term'] == 'Claude' and proof['dictionary_alias'] == 'cloud'
    assert candidate.count('Claude') == 1 and candidate.count('cloud') == 1
    assert candidate.index('Claude') != raw.index('cloud')  # Free rewrite changed the preceding length.


@pytest.mark.parametrize('candidate,reason', [
    ('日志先保存，再用cloud助手，cloud云服务。', 'llm_invalid_output'),
    ('日志先保存，再用cloud助手，Claude云服务。', 'llm_invalid_output'),
    ('日志先保存，再用Claude助手，Claude云服务。', 'protected_content_changed'),
])
def test_valid_derived_term_proof_cannot_validate_wrong_actual_candidate(candidate, reason):
    raw = '日志先保存，再用cloud助手，cloud云服务。'
    result, provider = run(raw, {'edits': [term_proof()], 'candidate_text': candidate}, terms=[TERM], retries=1)
    assert result.text == raw and result.review_reason == reason
    assert len(provider.calls) == 1


def test_self_correction_canonical_proof_and_complete_candidate_are_both_retained():
    raw = '重试3次，不对，是2次，每次间隔5秒。'
    candidate = '每次间隔5秒，重试2次。'
    wire = {'candidate_text': candidate, 'edits': [{'kind': 'self_correction',
        'before': '3次，不对，是2次', 'after': '2次', 'old': '3次', 'trigger': '不对', 'final': '2次'}]}
    result, provider = run(raw, wire)
    assert result.text == candidate and not result.review_required
    audit_input = canonical_evidence(raw, wire, [TERM] if 'cloud' in raw else [])
    assert audit_input['candidate'] == candidate
    proof = audit_input['verified_self_corrections'][0]
    assert raw[proof['source_start']:proof['source_end']] == '3次，不对，是2次'
    assert raw[proof['replacement_source_start']:proof['replacement_source_end']] == '2次'
    assert raw[proof['trigger']['start']:proof['trigger']['end']] == '不对'


@pytest.mark.parametrize('raw,candidate', [
    ('等待3.5秒。', '等待35秒。'),
    ('检查/var/cloud/app.log。', '检查/var/claw/app.log。'),
    ('打开https://cloud.example.com/api。', '打开https://claw.example.com/api。'),
    ('不要修改配置。', '修改配置。'),
    ('使用SQLite检查日志。', '使用sqlite检查日志。'),
])
def test_complete_candidate_has_no_protected_anchor_exemption(raw, candidate):
    result, provider = run(raw, {'edits': [], 'candidate_text': candidate}, retries=1)
    assert result.text == raw and result.review_reason == 'protected_content_changed'
    assert len(provider.calls) == 1


def test_proof_failure_is_not_repaired_from_valid_looking_candidate():
    raw = '嗯，检查日志。'
    wire = {'edits': [{'kind': 'filler', 'before': '呃，', 'after': ''}], 'candidate_text': '检查日志。'}
    result, provider = run(raw, wire, retries=1)
    assert result.text == raw and result.review_reason == 'llm_invalid_output'
    assert len(provider.calls) == 1


def test_supplied_raw_with_declared_filler_is_not_overwritten_by_derived():
    raw = '嗯，检查日志。'
    wire = {'edits': [{'kind': 'filler', 'before': '嗯，', 'after': ''}], 'candidate_text': raw}
    result, provider = run(raw, wire)
    assert result.text == raw and result.review_reason == 'llm_invalid_output'
    assert len(provider.calls) == 1  # Existing _looks_unrefined, not a new effect guard.


def test_optional_candidate_keeps_existing_declared_filler_effect_guard():
    raw = '嗯那个，帮我加一个按钮，然后然后记得不要改数据库'
    candidate = '嗯那个，帮我加一个按钮，然后记得不要改数据库'
    wire = {'edits': [{'kind': 'filler', 'before': '嗯那个', 'after': ''}],
            'candidate_text': candidate}
    result, provider = run(raw, wire, retries=1)
    assert [request.purpose for request, _ in provider.calls] == ['refine']
    assert provider.calls[0][0].input['transcript'] == raw
    assert len({deadline for _, deadline in provider.calls}) == 1
    assert result.text == raw and result.review_required is True
    assert result.review_reason == 'llm_invalid_output'


def test_protected_candidate_failure_does_not_fall_back_to_derived_text():
    raw = '嗯，检查日志，不要发送消息。'
    candidate = '检查日志并发送消息。'
    reject = {'decision': 'reject', 'violations': [
        {'category': 'fact', 'source_start': 2, 'source_end': 6, 'reason_code': 'added_action'}]}
    wire = {'edits': [{'kind': 'filler', 'before': '嗯，', 'after': ''}], 'candidate_text': candidate}
    result, provider = run(raw, wire, audit=reject, retries=1)
    assert result.text == raw and result.review_reason == 'protected_content_changed'
    assert [request.purpose for request, _ in provider.calls] == ['refine']
    assert provider.calls[0][0].input['transcript'] == raw


def test_optional_candidate_still_shares_one_schema_retry_with_audit():
    raw = '嗯，日志先保存。'
    wire = {'edits': [{'kind': 'filler', 'before': '嗯，', 'after': ''}], 'candidate_text': '先保存日志。'}
    provider = Answers({'edits': [], 'candidate_text': None}, wire, {'decision': 'uncertain', 'violations': []})
    result = asyncio.run(RefinerService(provider, edits_only_candidate=True).refine(raw))
    assert result.text == '先保存日志。' and not result.review_required
    assert [request.purpose for request, _ in provider.calls] == ['refine', 'refine']
    assert len({deadline for _, deadline in provider.calls}) == 1
    assert provider.responses == [{"decision":"uncertain","violations":[]}]


@pytest.mark.parametrize('candidate', ['先保存日志，再检查配置。', '先保存日志。\n再检查配置。'])
def test_layout_only_complete_candidate_keeps_original_conditional_audit(candidate):
    raw = '先保存日志 再检查配置'
    result, provider = run(raw, {'edits': [], 'candidate_text': candidate})
    assert result.text == candidate and not result.review_required
    assert len(provider.calls) == 1
