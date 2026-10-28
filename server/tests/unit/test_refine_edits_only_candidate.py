"""Internal protocol experiment only; fake responses do not prove model quality."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.core.clock import loop_time
from app.providers.llm.base import LlmResponse
from app.services.llm_validation import matches_schema
from app.services.refine_edits_only import (
    EDITS_ONLY_SCHEMA, EDITS_ONLY_SYSTEM, EDITS_ONLY_VERSION, freeze_terms, resolve_edits_only,
)
from app.services.refiner import RefinerService
from app.services.refine_evidence import validate_edits

VECTORS = json.loads((Path(__file__).parents[1] / 'fixtures/edits_only_vectors.json').read_text())
PASS = {'decision': 'pass', 'violations': []}


class Answers:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    async def generate(self, request, deadline):
        self.calls.append((request, deadline))
        assert self.responses, 'unexpected extra model invocation'
        return LlmResponse(parsed=deepcopy(self.responses.pop(0)), provider='stub', model_id='edits-only-fixture')


def run(raw, wire, *, terms=None, audit=None, automatic_retries=0):
    provider = Answers(wire, PASS if audit is None else audit)
    result = asyncio.run(RefinerService(provider, automatic_retries=automatic_retries,
        edits_only_candidate=True).refine(raw, terms))
    return result, provider


def self_wire(raw, old, final, trigger='不对'):
    start, end = raw.index(old), raw.index(final) + len(final)
    return {'edits': [{'kind': 'self_correction', 'before': raw,
        'after': raw[:start] + final + raw[end:], 'old': old, 'trigger': trigger, 'final': final}]}


def term():
    return {'id': '00000000-0000-0000-0000-000000000001', 'term': 'SQLite',
            'aliases': ['SQL lite'], 'ambiguous_alias_norms': [], 'contexts': []}


def term_wire(before, after, value=None):
    value = term() if value is None else value
    return {'edits': [{'kind': 'term_correction', 'before': before, 'after': after,
        'dictionary_term_id': value['id'], 'dictionary_alias': value['aliases'][0]}]}


@pytest.mark.parametrize('vector', VECTORS['positive'], ids=lambda row: row['id'])
def test_reviewed_positive_conversion_and_real_guard_path(vector):
    candidate, edits = resolve_edits_only(vector['raw'], vector['candidate_wire'], vector['terms'])
    assert candidate == vector['expected_reconstruction']
    for edit in edits:
        assert vector['raw'][edit['source_start']:edit['source_end']] == edit['source_text']
    result, provider = run(vector['raw'], vector['candidate_wire'], terms=vector['terms'])
    assert result.text == candidate and result.review_required is False
    expected = ['refine']  # User removed the second model request.
    assert [request.purpose for request, _ in provider.calls] == expected
    assert provider.calls[0][0].schema == EDITS_ONLY_SCHEMA
    assert provider.calls[0][0].system_prompt == EDITS_ONLY_SYSTEM
    assert provider.calls[0][0].system_prompt_version == provider.calls[0][0].output_schema_id == EDITS_ONLY_VERSION
    assert len({deadline for _, deadline in provider.calls}) == 1
    if 'expected_actual_source_codepoint_span' in vector:
        start, end = vector['expected_actual_source_codepoint_span']
        proof = validate_edits(vector['raw'], candidate, edits, vector['terms'])[2][0]
        assert (proof['source_start'], proof['source_end']) == (start, end)


@pytest.mark.parametrize('raw,before,after,expected,span', [
    ('🙂嗯，检查日志。', '🙂嗯，检查', '🙂检查', '🙂检查日志。', (1, 3)),
    ('嗯，甲；嗯，乙。', '嗯，乙', '乙', '嗯，甲；乙。', (4, 6)),
    ('第一行\n嗯，第二行', '\n嗯，第二行', '\n第二行', '第一行\n第二行', (4, 6)),
    ('嗯  检查日志', '嗯  检查', '检查', '检查日志', (0, 3)),
])
def test_unique_context_splice_preserves_exact_unicode_layout(raw, before, after, expected, span):
    wire = {'edits': [{'kind': 'filler', 'before': before, 'after': after}]}
    candidate, edits = resolve_edits_only(raw, wire, [])
    assert candidate == expected
    assert (edits[0]['source_start'], edits[0]['source_end']) == span
    result, _ = run(raw, wire)
    assert result.text == expected and not result.review_required


@pytest.mark.parametrize('raw,wire', [
    ('aaaa', {'edits': [{'kind': 'filler', 'before': 'aaa', 'after': ''}]}),
    ('嗯嗯', {'edits': [{'kind': 'filler', 'before': '嗯嗯', 'after': '嗯'}]}),
    ('嗯，甲；嗯，乙', {'edits': [{'kind': 'filler', 'before': '嗯，', 'after': ''}]}),
    ('嗯  检查', {'edits': [{'kind': 'filler', 'before': '嗯 检查', 'after': '检查'}]}),
    ('嗯，检查', {'edits': [{'kind': 'filler', 'before': '嗯，检查', 'after': '审核'}]}),
    ('嗯，检查', {'edits': [{'kind': 'filler', 'before': '嗯，', 'after': ''},
                          {'kind': 'format', 'before': '嗯，检查', 'after': '嗯，检查。'}]}),
    ('保存日志保存日志', {'edits': [{'kind': 'repetition', 'before': '保存日志保存日志', 'after': ''}]}),
])
def test_bad_or_ambiguous_reference_never_gets_a_retry_or_audit(raw, wire):
    result, provider = run(raw, wire, automatic_retries=1)
    assert result.text == raw and result.review_reason == 'llm_invalid_output'
    assert len(provider.calls) == 1


@pytest.mark.parametrize('raw,old,final,trigger', [
    ('等待3秒，不对，是5 秒。', '3秒', '5 ', '不对'),
    ('等待3秒，不对，是5 毫秒。', '3秒', '5 毫', '不对'),
    ('等待3秒，不对，是5秒。', '3秒', '5', '不对'),
    ('等待3秒，不对，是至少5秒。', '3秒', '5秒', '不对'),
    ('保存日志，不对，是不要删除日志。', '保存日志', '删除日志', '不对'),
    ('等待3秒，不对，是5秒。', '3', '5秒', '不对'),
    ('路径/var/cloud，不对，改用/var/server。', 'cloud', '/var/server', '不对'),
    ('等待3次，再2次。', '3次', '2次', '再'),
    ('等待3次，是2次。', '3次', '2次', '是'),
    ('等待3次，保留日志，不对，是2次。', '3次', '2次', '不对'),
])
def test_self_constraints_cannot_be_hidden_by_wrapper(raw, old, final, trigger):
    result, provider = run(raw, self_wire(raw, old, final, trigger))
    assert result.text == raw and result.review_reason == 'llm_invalid_output'
    assert len(provider.calls) == 1


@pytest.mark.parametrize('final', ['5 秒', '5 毫秒', '至少5秒', '不要等待5秒', '五毫秒', '5 ms'])
def test_complete_final_units_and_constraints_reach_original_audit(final):
    raw = f'等待3秒，不对，是{final}。'
    result, provider = run(raw, self_wire(raw, '3秒', final))
    assert result.text == f'等待{final}。' and not result.review_required
    assert [request.purpose for request, _ in provider.calls] == ['refine']
    candidate, proofs = resolve_edits_only(raw, self_wire(raw, '3秒', final), [])
    proof = validate_edits(raw, candidate, proofs, [])[1][0]
    assert raw[proof['replacement_source_start']:proof['replacement_source_end']] == final


def test_self_final_and_old_must_be_unique_inside_before():
    raw = '重试3次，不对，2次加2次'
    wire = {'edits': [{'kind': 'self_correction', 'before': raw, 'after': '重试2次加2次',
                      'old': '3次', 'trigger': '不对', 'final': '2次'}]}
    result, provider = run(raw, wire)
    assert result.review_reason == 'llm_invalid_output' and len(provider.calls) == 1


@pytest.mark.parametrize('kind,before,after,reason', [
    ('format', '3.5', '35', 'protected_content_changed'),
    ('filler', '.', '', 'llm_invalid_output'),
    ('format', '/var/log/app.log', '/var/log/applog', 'protected_content_changed'),
])
def test_format_does_not_rewrite_guard_baseline(kind, before, after, reason):
    raw = '等待3.5秒，检查/var/log/app.log'
    result, provider = run(raw, {'edits': [{'kind': kind, 'before': before, 'after': after}]})
    assert result.text == raw and result.review_reason == reason and len(provider.calls) == 1


@pytest.mark.parametrize('raw', ['“嗯，检查日志”', '`嗯，检查日志`'])
def test_literal_filler_cannot_hide_in_bigger_wrapper(raw):
    wire = {'edits': [{'kind': 'filler', 'before': raw, 'after': raw.replace('嗯，', '')}]}
    result, provider = run(raw, wire)
    assert result.review_reason == 'llm_invalid_output' and len(provider.calls) == 1


def test_terms_use_one_narrow_position_after_an_earlier_splice():
    raw = '嗯，使用 SQL lite 库，另有 SQL lite 字样。'
    wire = term_wire('使用 SQL lite 库', '使用 SQLite 库')
    wire['edits'].insert(0, {'kind': 'filler', 'before': '嗯，', 'after': ''})
    result, provider = run(raw, wire, terms=[term()])
    assert result.text == '使用 SQLite 库，另有 SQL lite 字样。' and not result.review_required
    candidate, proofs = resolve_edits_only(raw, wire, [term()])
    evidence = validate_edits(raw, candidate, proofs, [term()])[2][0]
    assert evidence['source_text'] == 'SQL lite' and evidence['source_start'] == raw.index('SQL lite')


@pytest.mark.parametrize('change', ['id', 'alias', 'after', 'duplicate_id', 'shared_alias', 'ambiguous_alias'])
def test_frozen_dictionary_identity_mismatch_is_not_repaired(change):
    raw, terms = '使用 SQL lite 库。', [term()]
    wire = term_wire(raw, '使用 SQLite 库。')
    if change == 'id':
        wire['edits'][0]['dictionary_term_id'] = '00000000-0000-0000-0000-000000000002'
    elif change == 'alias':
        wire['edits'][0]['dictionary_alias'] = 'sql lite'
    elif change == 'after':
        wire['edits'][0]['after'] = '使用 Postgres 库。'
    elif change == 'duplicate_id':
        terms.append(deepcopy(terms[0]))
    elif change == 'shared_alias':
        other = deepcopy(terms[0]); other['id'] = '00000000-0000-0000-0000-000000000002'
        terms.append(other)
    else:
        terms[0]['ambiguous_alias_norms'] = ['sql lite']
    result, provider = run(raw, wire, terms=terms)
    assert result.text == raw and result.review_reason == 'llm_invalid_output'
    assert len(provider.calls) == (0 if change == 'duplicate_id' else 1)


@pytest.mark.parametrize('literal', ['/var/cloud/app.log', 'https://cloud.example.com/api', 'cloud.example.com/api'])
def test_term_inside_full_path_or_url_is_rejected(literal):
    value = dict(term(), term='Claude', aliases=['cloud'])
    raw = f'检查{literal}，保留原值。'
    result, provider = run(raw, term_wire(raw, raw.replace('cloud', 'Claude'), value), terms=[value])
    assert result.text == raw and result.review_reason == 'llm_invalid_output' and len(provider.calls) == 1


def test_unique_ordinary_alias_correction_no_longer_gets_a_second_audit():
    value = dict(term(), term='Claude', aliases=['cloud'])
    raw = '使用cloud云服务。'
    audit = {'decision': 'reject', 'violations': [{'category': 'proper_term', 'source_start': 2,
              'source_end': 7, 'reason_code': 'ORDINARY_SENSE'}]}
    result, provider = run(raw, term_wire(raw, '使用Claude云服务。', value), terms=[value], audit=audit)
    assert result.text == '使用Claude云服务。' and not result.review_required and len(provider.calls) == 1


def test_input_and_provider_mutations_cannot_change_frozen_validation_terms():
    terms = [term()]
    wire = term_wire('使用 SQL lite 库。', '使用 SQLite 库。')

    class Mutator(Answers):
        async def generate(self, request, deadline):
            if request.purpose == 'refine':
                assert request.input['terms'] is not terms
                terms[0]['term'] = 'Postgres'
                request.input['terms'][0]['term'] = 'MySQL'
            return await super().generate(request, deadline)

    provider = Mutator(wire, PASS)
    result = asyncio.run(RefinerService(provider, edits_only_candidate=True).refine('使用 SQL lite 库。', terms))
    assert result.text == '使用 SQLite 库。' and not result.review_required
    assert [r.purpose for r, _ in provider.calls] == ['refine']
    assert provider.responses == [PASS]


@pytest.mark.parametrize('extra', [{'candidate': '检查'}, {'source_start': 0}, {'unknown': True}])
def test_top_level_unknown_fields_fail_schema(extra):
    assert not matches_schema({'edits': [], **extra}, EDITS_ONLY_SCHEMA)


def test_cross_kind_and_missing_fields_and_nonunique_oneof_rejected():
    invalid = {'edits': [{'kind': 'filler', 'before': '嗯，', 'after': '', 'trigger': '不对'}]}
    assert not matches_schema(invalid, EDITS_ONLY_SCHEMA)
    assert not matches_schema({'edits': [{'kind': 'self_correction', 'before': '3次不对2次', 'after': '2次'}]}, EDITS_ONLY_SCHEMA)
    assert not matches_schema('text', {'oneOf': [{'type': 'string'}, {'type': 'string'}]})
    assert not matches_schema('text', {'oneOf': []})
    assert not matches_schema('text', {'oneOf': [{'type': 'string'}], 'enum': ['different']})


@pytest.mark.parametrize('fields', [
    {'source_start': 0}, {'source_end': 2}, {'candidate_text': '检查日志。'},
    {'dictionary_term': 'SQLite'}, {'occurrence': 1}, {'left': ''},
])
def test_no_old_offsets_or_extra_protocol_fields_are_accepted(fields):
    wire = {'edits': [{'kind': 'filler', 'before': '嗯，', 'after': '', **fields}]}
    assert not matches_schema(wire, EDITS_ONLY_SCHEMA)
    result, provider = run('嗯，检查日志。', wire)
    assert result.review_reason == 'llm_invalid_output' and len(provider.calls) == 1


def test_provider_cannot_mutate_request_schema_to_disable_canonical_conversion_check():
    class SchemaMutator(Answers):
        async def generate(self, request, deadline):
            request.schema.clear()
            return await super().generate(request, deadline)

    provider = SchemaMutator({'edits': [], 'candidate_text': None})
    result = asyncio.run(RefinerService(provider, edits_only_candidate=True).refine('检查日志。'))
    assert result.text == '检查日志。' and result.review_reason == 'llm_invalid_output'
    assert len(provider.calls) == 1 and 'properties' in EDITS_ONLY_SCHEMA


def test_one_shared_structural_retry_never_extends_deadline_or_retries_audit_again():
    invalid = {'edits': [], 'extra': True}
    valid = {'edits': [{'kind': 'filler', 'before': '嗯，', 'after': ''}]}
    provider = Answers(invalid, valid, {'decision': 'uncertain', 'violations': []})
    result = asyncio.run(RefinerService(provider, edits_only_candidate=True).refine('嗯，检查日志。'))
    assert result.text == '检查日志。' and not result.review_required
    assert [request.purpose for request, _ in provider.calls] == ['refine', 'refine']
    assert len({deadline for _, deadline in provider.calls}) == 1
    assert len(provider.responses) == 1


def test_deadline_and_cancel_propagation_no_extra_call():
    async def exercise():
        provider = Answers({'edits': []})
        result = await RefinerService(provider, edits_only_candidate=True).refine('检查日志。', deadline=loop_time()-1)
        assert result.review_reason == 'budget_exceeded' and not provider.calls
        with pytest.raises(TimeoutError):
            resolve_edits_only('检查日志。', {'edits': []}, [], deadline=loop_time()-1)

        class Cancelled:
            async def generate(self, request, deadline):
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await RefinerService(Cancelled(), edits_only_candidate=True).refine('检查日志。')
    asyncio.run(exercise())


def test_default_path_still_allows_anchored_filler_plus_natural_order():
    raw = '嗯，日志先保存，再检查配置'
    provider = Answers({'candidate_text': '先保存日志，再检查配置', 'edits': [
        {'kind': 'filler', 'source_start': 0, 'source_end': 2, 'source_text': '嗯，', 'replacement_text': ''}]}, PASS)
    result = asyncio.run(RefinerService(provider).refine(raw))
    assert result.text == '先保存日志，再检查配置' and not result.review_required
    assert provider.calls[0][0].output_schema_id == 'refine-v2-terms'
    assert 'candidate_text' in provider.calls[0][0].schema['properties']


def test_empty_edits_not_claimed_as_quality_acceptance_for_explicit_correction():
    # Preserves the existing heuristic; this documented false-negative is NOT
    # counted as T03 quality success. Real paired fixtures retain their oracle.
    raw = '数据库用 MySQL，不对，改用 Postgres，但是前端不变'
    result, provider = run(raw, {'edits': []})
    assert result.text == raw and len(provider.calls) == 1


@pytest.mark.parametrize('malformed', [
    {'decision': 'pass', 'violations': {}},
    {'decision': 'pass', 'violations': [], 'extra': True},
])
def test_unused_audit_schema_payload_never_creates_a_second_request(malformed):
    class AuditSchemaMutator(Answers):
        async def generate(self, request, deadline):
            if request.purpose == 'audit':
                request.schema.clear()
            return await super().generate(request, deadline)

    raw = '嗯，检查日志。'
    provider = AuditSchemaMutator(
        {'edits': [{'kind': 'filler', 'before': '嗯，', 'after': ''}]}, malformed)
    result = asyncio.run(RefinerService(provider, edits_only_candidate=True).refine(raw))
    assert result.text == '检查日志。' and not result.review_required
    assert [request.purpose for request, _ in provider.calls] == ['refine']
    assert len({deadline for _, deadline in provider.calls}) == 1
    assert len(provider.responses) == 1


@pytest.mark.parametrize('raw,canonical,expected', [
    ('启动我的助手 cloud 来检查代码，再比较 cloud 云服务的价格', 'claw',
     '启动我的助手 claw 来检查代码，再比较 claw 云服务的价格'),
    ('cloud，cloud', 'cloudDesk', 'cloudDesk，cloudDesk'),
    ('cloud🙂cloud', 'claw', 'claw🙂claw'),
])
def test_same_frozen_term_batch_expands_to_exact_original_positions(raw, canonical, expected):
    value = {'id': '5fa1d6fd-4304-4f89-8405-776b38432db0', 'term': canonical,
             'aliases': ['cloud'], 'contexts': [], 'ambiguous_alias_norms': []}
    wire = term_wire(raw, expected, value)
    candidate, proof = resolve_edits_only(raw, wire, [value])
    assert candidate == expected and len(proof) == 2
    assert [raw[e['source_start']:e['source_end']] for e in proof] == ['cloud', 'cloud']
    result, provider = run(raw, wire, terms=[value])
    assert result.text == expected and not result.review_required
    assert [r.purpose for r, _ in provider.calls] == ['refine']


@pytest.mark.parametrize('raw,canonical', [
    ('“cloud” cloud', 'claw'),
    ('https://cloud.example/cloud cloud', 'claw'),
    ('/var/cloud cloud', 'claw'),
    ('clouded cloud', 'claw'),
    ('cloud cloud', 'claw2'),
    ('cloud cloud', '不要'),
    (' cloud' * 65, 'claw'),
])
def test_batch_dictionary_does_not_bypass_original_protected_boundaries(raw, canonical):
    value = {'id': 'batch-owned', 'term': canonical, 'aliases': ['cloud']}
    result, provider = run(raw, term_wire(raw, raw.replace('cloud', canonical), value), terms=[value])
    assert result.text == raw and result.review_required
    assert len(provider.calls) == 1


def test_batch_dictionary_cannot_hide_an_unrelated_text_change():
    raw = 'cloud 保存日志，cloud 保留数据库'
    value = {'id': 'batch-owned', 'term': 'claw', 'aliases': ['cloud']}
    result, provider = run(raw, term_wire(raw, 'claw 删除日志，claw 保留数据库', value), terms=[value])
    assert result.text == raw and result.review_required and len(provider.calls) == 1
