"""Internal no-op tolerance controls, not real model fidelity evidence."""
from copy import deepcopy

import pytest

from app.services.refine_edits_only import resolve_edits_only
from tests.unit.test_optional_candidate_adapter import run, canonical_evidence


def noop(kind, raw):
    value = {'kind': kind, 'before': raw, 'after': raw}
    if kind == 'self_correction':
        # Shape-valid claims are deliberately not accepted as semantic proof.
        value.update(old='not a reference', trigger='not a correction', final='not a reference')
    if kind == 'term_correction':
        value.update(dictionary_term_id='absent-id', dictionary_alias='absent-alias')
    return value


@pytest.mark.parametrize('kind', ['filler', 'repetition', 'format', 'self_correction', 'term_correction'])
def test_exact_noop_has_no_canonical_edit_or_exemption(kind):
    raw = '请保留纸张。'
    wire = {'edits': [noop(kind, raw)]}
    original = deepcopy(wire)
    selected, canonical = resolve_edits_only(raw, wire, [])
    assert selected == raw and canonical == [] and wire == original
    result, provider = run(raw, wire)
    assert result.text == raw and not result.review_required
    assert [request.purpose for request, _ in provider.calls] == ['refine']


@pytest.mark.parametrize('raw,wire', [
    ('甲甲', {'edits': [noop('filler', '甲')]}),
    ('甲乙', {'edits': [noop('filler', '甲丙')]}),
    ('甲乙', {'edits': [dict(noop('filler', '甲乙'), extra='not allowed')]}),
    ('甲乙', {'edits': [{'kind': 'filler', 'before': '甲乙', 'after': None}]}),
    ('甲乙', {'edits': [noop('unknown-kind', '甲乙')]}),
    ('甲乙', {'edits': [noop('filler', '甲乙')] * 65}),
    ('甲乙', {'edits': [noop('filler', '甲乙')], 'candidate_text': None}),
])
def test_noop_tolerance_never_precedes_original_shape_and_unique_quote_guards(raw, wire):
    with pytest.raises(ValueError):
        resolve_edits_only(raw, wire, [])
    result, provider = run(raw, wire)
    assert result.text == raw and result.review_reason == 'llm_invalid_output'
    assert len(provider.calls) == 1


def test_noop_does_not_relax_before_reference_overlap():
    raw = '嗯，请保留纸张。'
    wire = {'edits': [noop('filler', raw), {'kind': 'filler', 'before': '嗯，', 'after': ''}]}
    with pytest.raises(ValueError, match='OVERLAPPING_BEFORE_REFERENCES'):
        resolve_edits_only(raw, wire, [])


def test_noop_and_disjoint_real_edit_keep_original_unicode_positions():
    raw = '🙂记录纸张；嗯，检查标签。'
    wire = {'edits': [noop('filler', '🙂记录纸张'), {'kind': 'filler', 'before': '嗯，', 'after': ''}]}
    before = deepcopy(wire)
    selected, canonical = resolve_edits_only(raw, wire, [])
    assert selected == '🙂记录纸张；检查标签。'
    assert len(canonical) == 1
    assert canonical[0]['source_start'] == raw.index('嗯，')
    assert raw[canonical[0]['source_start']:canonical[0]['source_end']] == '嗯，'
    assert wire == before
    result, provider = run(raw, wire)
    assert result.text == selected and not result.review_required
    audit_input = canonical_evidence(raw, wire)
    assert audit_input['raw'] == raw and audit_input['candidate'] == selected
    assert audit_input['verified_self_corrections'] == audit_input['verified_term_corrections'] == []


@pytest.mark.parametrize('raw,candidate,kind,terms', [
    ('等待 6 秒，不对，改为 9 秒。', '等待 9 秒。', 'self_correction', []),
    ('打开 /var/notes/a.b。', '打开 /var/notes/ab。', 'format', []),
    ('访问 https://notes.example.net/v1。', '访问 https://notes.example.net/v2。', 'format', []),
    ('不要修改纸张。', '修改纸张。', 'filler', []),
    ('打开 pebble 面板。', '打开 PebbleNote 面板。', 'term_correction',
     [{'id': 'absent-id', 'term': 'PebbleNote', 'aliases': ['absent-alias'], 'contexts': []}]),
])
def test_noop_never_authorizes_protected_change_in_supplied_candidate(raw, candidate, kind, terms):
    wire = {'edits': [noop(kind, raw)], 'candidate_text': candidate}
    selected, canonical = resolve_edits_only(raw, wire, terms)
    assert selected == candidate and canonical == []  # Do not repair supplied text.
    result, provider = run(raw, wire, terms=terms, retries=1)
    assert result.text == raw and result.review_reason == 'protected_content_changed'
    assert len(provider.calls) == 1


def test_removed_semantic_audit_is_not_resurrected_by_noop_metadata():
    raw, candidate = '收好画笔。', '收好调色盘。'
    reject = {'decision': 'reject', 'violations': [{'category': 'fact',
        'source_start': 2, 'source_end': 4, 'reason_code': 'object_changed'}]}
    wire = {'edits': [noop('filler', raw)], 'candidate_text': candidate}
    result, provider = run(raw, wire, audit=reject, retries=1)
    assert result.text == candidate and not result.review_required
    assert [request.purpose for request, _ in provider.calls] == ['refine']
    audited = canonical_evidence(raw, wire)
    assert audited['candidate'] == candidate and audited['raw'] == raw
    assert audited['verified_self_corrections'] == audited['verified_term_corrections'] == []
    assert len({deadline for _, deadline in provider.calls}) == 1


def test_noop_does_not_hide_raw_filler_or_create_a_missing_edit():
    raw = '嗯，请检查纸张。'
    result, provider = run(raw, {'edits': [noop('filler', raw)]})
    assert result.text == raw and result.review_reason == 'llm_invalid_output'
    assert len(provider.calls) == 1


def test_near_equal_strings_are_not_noops():
    with pytest.raises(ValueError):
        resolve_edits_only('纸张', {'edits': [{'kind': 'filler', 'before': '纸张', 'after': '紙張'}]}, [])


def test_duplicate_frozen_ids_still_fail_even_with_only_noop_metadata():
    raw = '请保留纸张。'
    term = {'id': 'same', 'term': 'Papyrus', 'aliases': ['paper'], 'contexts': []}
    with pytest.raises(ValueError, match='TERM_SNAPSHOT_ID_NOT_UNIQUE'):
        resolve_edits_only(raw, {'edits': [noop('filler', raw)]}, [term, deepcopy(term)])
