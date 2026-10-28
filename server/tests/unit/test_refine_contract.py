"""Contract/control-flow regressions, not real model semantic acceptance."""
import asyncio
import uuid
import pytest
from app.providers.llm.base import LlmResponse
from app.services.refiner import RefinerService


class Answers:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []
    async def generate(self, request, deadline):
        self.calls.append((request, deadline))
        return LlmResponse(parsed=self.answers.pop(0), provider='stub', model_id='fixture')


def run(raw, candidate, edits=(), *, audit=None, terms=None):
    provider = Answers({'candidate_text': candidate, 'edits': list(edits)}, audit or {'decision': 'pass', 'violations': []})
    result = asyncio.run(RefinerService(provider, automatic_retries=0).refine(raw, terms))
    return result, provider


def edit(raw, source, replacement, kind='filler', **fields):
    start = raw.index(source)
    return dict(kind=kind, source_start=start, source_end=start+len(source), source_text=source,
                replacement_text=replacement, **fields)


def test_unicode_offsets_are_codepoints_not_utf8_or_utf16():
    raw = '🙂嗯，请检查按钮。'
    result, _ = run(raw, '🙂请检查按钮。', [edit(raw, '嗯，', '')])
    assert result.text == '🙂请检查按钮。' and not result.review_required
    malformed = edit(raw, '嗯，', ''); malformed['source_start'] = 2
    result, provider = run(raw, '🙂请检查按钮。', [malformed])
    assert result.review_reason == 'llm_invalid_output' and len(provider.calls) == 1


def test_self_correction_requires_both_trigger_and_final_position():
    raw = '重试3次，不对，是2次，间隔5秒。'
    proof = edit(raw, '3次，不对，是2次', '2次', 'self_correction',
        trigger={'start': raw.index('不对'), 'end': raw.index('不对')+2, 'text': '不对'},
        replacement_source_start=raw.index('2次'), replacement_source_end=raw.index('2次')+2)
    result, provider = run(raw, '重试2次，间隔5秒。', [proof])
    assert not result.review_required and result.text == '重试2次，间隔5秒。'
    assert [r.purpose for r, _ in provider.calls] == ['refine']
    invalid = dict(proof, replacement_source_start=0)
    result, provider = run(raw, '重试2次，间隔5秒。', [invalid])
    assert result.review_reason == 'llm_invalid_output' and len(provider.calls) == 1


def test_sequence_is_not_self_correction_and_other_number_is_never_exempted():
    raw = '先3次再2次，间隔5秒。'
    proof = edit(raw, '3次再2次', '2次', 'self_correction',
        trigger={'start': raw.index('再'), 'end': raw.index('再')+1, 'text': '再'},
        replacement_source_start=raw.index('2次'), replacement_source_end=raw.index('2次')+2)
    result, _ = run(raw, '先2次，间隔5秒。', [proof])
    assert result.review_reason == 'llm_invalid_output'
    raw = '重试3次，不对，是2次，间隔5秒。'
    proof = edit(raw, '3次，不对，是2次', '2次', 'self_correction',
        trigger={'start': raw.index('不对'), 'end': raw.index('不对')+2, 'text': '不对'},
        replacement_source_start=raw.index('2次'), replacement_source_end=raw.index('2次')+2)
    result, _ = run(raw, '重试2次，间隔6秒。', [proof])
    assert result.review_reason == 'protected_content_changed'


def term():
    return {'id': str(uuid.UUID(int=1)), 'term': 'Claude', 'norm': 'claude', 'aliases': ['cloud'], 'contexts': [], 'ambiguous_alias_norms': []}


def term_edit(raw, source='cloud', *, start=None):
    t = term()
    value = edit(raw, source, t['term'], 'term_correction', dictionary_term_id=t['id'], dictionary_term=t['term'], dictionary_alias='cloud')
    if start is not None:
        value.update(source_start=start, source_end=start+len(source))
    return value


def test_dictionary_correction_is_local_and_audited_with_frozen_identity():
    raw = '我用cloud助手编程，cloud云服务。'
    candidate = '我用Claude助手编程，cloud云服务。'
    result, provider = run(raw, candidate, [term_edit(raw)], terms=[term()])
    assert not result.review_required and result.text == candidate
    assert [r.purpose for r, _ in provider.calls] == ['refine']
    assert provider.calls[0][0].input['terms'] == [term()]
    # Same global token counts but the wrong occurrence is not authorized.
    result, provider = run(raw, '我用cloud助手编程，Claude云服务。', [term_edit(raw)], terms=[term()])
    assert result.review_reason == 'llm_invalid_output' and len(provider.calls) == 1


def test_unique_alias_is_allowed_in_ordinary_context_but_ambiguous_is_not():
    raw = '使用cloud云服务。'
    rejection = {'decision': 'reject', 'violations': [{'category': 'proper_term', 'source_start': 2,
        'source_end': 7, 'reason_code': 'ORDINARY_SENSE'}]}
    result, _ = run(raw, '使用Claude云服务。', [term_edit(raw)], terms=[term()], audit=rejection)
    assert result.text == '使用Claude云服务。' and not result.review_required
    assert len(_.calls) == 1
    ambiguous = term(); ambiguous['ambiguous_alias_norms'] = ['cloud']
    result, provider = run(raw, '使用Claude云服务。', [term_edit(raw)], terms=[ambiguous])
    assert result.review_reason == 'llm_invalid_output' and len(provider.calls) == 1


@pytest.mark.parametrize('audit', [
    {'decision': 'uncertain', 'violations': []},
    {'decision': 'faithful', 'reason': 'old schema'},
    {'decision': 'pass', 'violations': [{'category': 'fact', 'source_start': 0, 'source_end': 1, 'reason_code': 'BAD'}]},
    {'decision': 'reject', 'violations': []},
])
def test_legacy_audit_payload_is_not_requested_or_consumed(audit):
    raw = '帮我检查一下这个按钮'
    result, _ = run(raw, '请检查这个按钮', audit=audit)
    assert not result.review_required and result.text == '请检查这个按钮'
    assert len(_.calls) == 1


def test_unanchored_english_case_change_is_protected():
    result, provider = run('使用 Docker。', '使用 docker。')
    assert result.review_reason == 'protected_content_changed' and len(provider.calls) == 1


@pytest.mark.parametrize('source,replacement', [('3.5', '35'), ('.', '')])
def test_format_annotation_cannot_erase_decimal_protection(source, replacement):
    raw = '等待3.5秒然后重试'
    result, provider = run(raw, '等待35秒，然后重试。', [edit(raw, source, replacement, 'format')])
    assert result.text == raw and result.review_required
    assert result.review_reason == 'protected_content_changed'
    assert len(provider.calls) == 1  # Reject before a permissive audit can mask it.


def test_punctuation_only_filler_cannot_erase_a_decimal_anchor():
    raw = '等待3.5秒然后重试'
    result, provider = run(raw, '等待35秒，然后重试。', [edit(raw, '.', '', 'filler')])
    assert result.text == raw and result.review_required
    assert result.review_reason == 'llm_invalid_output'
    assert len(provider.calls) == 1


def test_spoken_filler_and_adjacent_punctuation_remain_removable():
    raw = '嗯，等待3.5秒然后重试'
    result, provider = run(raw, '等待3.5秒，然后重试。', [edit(raw, '嗯，', '', 'filler')])
    assert result.text == '等待3.5秒，然后重试。' and not result.review_required
    assert [call[0].purpose for call in provider.calls] == ['refine']


@pytest.mark.parametrize('literal', [
    '/var/cloud/app.log',
    r'C:\var\cloud\app.log',
    'https://cloud.example.com/api',
    'https://api.cloud.example.com/',
    'https://cloud/',
    'cloud.example.com/api',
])
def test_term_alias_inside_original_path_or_url_is_not_a_word_correction(literal):
    raw = '请检查' + literal + '，保留原值。'
    start = raw.index('cloud')
    candidate = raw[:start] + 'Claude' + raw[start+len('cloud'):]
    result, provider = run(raw, candidate, [term_edit(raw)], terms=[term()])
    assert result.text == raw and result.review_required
    assert result.review_reason == 'llm_invalid_output'
    assert len(provider.calls) == 1


@pytest.mark.parametrize('candidate,replacement', [('等待3.5秒，然后重试。', '秒，'),
                                                 ('等待3.5秒。\n然后重试。', '秒。\n')])
def test_formatting_near_unchanged_decimal_still_succeeds_without_audit(candidate, replacement):
    raw = '等待3.5秒然后重试'
    result, provider = run(raw, candidate, [edit(raw, '秒', replacement, 'format')])
    assert result.text == candidate and not result.review_required
    assert len(provider.calls) == 1


def test_spaced_dictionary_alias_remains_eligible_for_position_correction():
    raw, candidate = '请使用SQL lite数据库。', '请使用SQLite数据库。'
    frozen = dict(term(), term='SQLite', norm='sqlite', aliases=['SQL lite'])
    proof = edit(raw, 'SQL lite', 'SQLite', 'term_correction', dictionary_term_id=frozen['id'],
                 dictionary_term='SQLite', dictionary_alias='SQL lite')
    result, provider = run(raw, candidate, [proof], terms=[frozen])
    assert result.text == candidate and not result.review_required
    assert [call[0].purpose for call in provider.calls] == ['refine']


@pytest.mark.parametrize('literal', ['/var/cloud/app.log', 'https://cloud.example.com/api'])
def test_literal_overlap_guard_does_not_block_separate_standalone_alias(literal):
    raw = f'检查{literal}，再用cloud助手编程。'
    start = raw.rindex('cloud')
    candidate = raw[:start] + 'Claude' + raw[start+len('cloud'):]
    result, provider = run(raw, candidate, [term_edit(raw, start=start)], terms=[term()])
    assert result.text == candidate and not result.review_required
    assert len(provider.calls) == 1


@pytest.mark.parametrize('old,new', [('3.5秒', '35秒'), ('/var/cloud/app.log', '/var/Claude/app.log'),
                                      ('https://cloud.example.com', 'https://Claude.example.com')])
def test_explicit_self_correction_keeps_its_separate_evidence_semantics(old, new):
    raw = f'请用{old}，不对，改成{new}。'
    start = raw.index(old)
    replacement_start = raw.index(new, start+len(old))
    proof = edit(raw, raw[start:replacement_start+len(new)], new, 'self_correction',
        trigger={'start': raw.index('不对'), 'end': raw.index('不对')+2, 'text': '不对'},
        replacement_source_start=replacement_start, replacement_source_end=replacement_start+len(new))
    result, provider = run(raw, f'请用{new}。', [proof])
    assert result.text == f'请用{new}。' and not result.review_required
    assert [call[0].purpose for call in provider.calls] == ['refine']
    assert provider.calls[0][0].input['transcript'] == raw
