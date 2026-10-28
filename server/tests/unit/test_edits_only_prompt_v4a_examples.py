"""Nine prompt examples through actual code; no model or audit quality claim."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.core.clock import loop_time
from app.providers.llm.base import LlmResponse
from app.services.refiner import RefinerService
from app.services.llm_validation import matches_schema
from app.services.refine_edits_only import EDITS_ONLY_SCHEMA, freeze_terms, resolve_edits_only
from app.services.refine_evidence import protected_problem, validate_edits


EXAMPLES = json.loads(
    (Path(__file__).parents[1] / 'fixtures/prompt_v4a_examples.json').read_text()
)


@pytest.mark.asyncio
@pytest.mark.parametrize('example', EXAMPLES['positive'], ids=lambda example: example['id'])
async def test_prompt_v4a_example_uses_actual_converter_and_protected_guards(example):
    original = deepcopy(example)
    raw, terms, wire = example['input']['transcript'], example['input']['terms'], example['output']
    deadline = loop_time() + 30
    assert matches_schema(wire, EDITS_ONLY_SCHEMA)
    frozen = freeze_terms(terms, deadline=deadline)
    candidate, evidence = resolve_edits_only(raw, wire, frozen, deadline=deadline)
    assert candidate == example['expected_candidate']
    assert [edit['kind'] for edit in evidence] == [edit['kind'] for edit in wire['edits']]
    for edit in evidence:
        assert raw[edit['source_start']:edit['source_end']] == edit['source_text']
    effective, corrections, term_evidence = validate_edits(raw, candidate, evidence, frozen)
    assert protected_problem(effective, candidate) is False
    assert len(corrections) == sum(edit['kind'] == 'self_correction' for edit in wire['edits'])
    assert len(term_evidence) == sum(edit['kind'] == 'term_correction' for edit in wire['edits'])
    if term_evidence:
        assert term_evidence[0]['dictionary_term_id'] == terms[0]['id']
        assert '再找 quartz 石英标本' in candidate
    if corrections:
        item = next(edit for edit in wire['edits'] if edit['kind'] == 'self_correction')
        proof = corrections[0]
        assert proof['replacement_text'] == item['final']
        assert raw[proof['replacement_source_start']:proof['replacement_source_end']] == item['final']
        assert raw[proof['trigger']['start']:proof['trigger']['end']] == item['trigger']
    if example['id'] == 'external_filler_and_quoted_literal':
        assert '“唔，别拆封”' in candidate
    if example['id'] == 'intentional_emphasis_noop':
        assert candidate == raw and evidence == []
    assert example == original


@pytest.mark.asyncio
@pytest.mark.parametrize('example', EXAMPLES['negative'], ids=lambda example: example['id'])
async def test_prompt_v4a_negative_cannot_cross_existing_guards(example):
    original = deepcopy(example)
    raw, terms, wire = example['input']['transcript'], example['input']['terms'], example['output']
    deadline = loop_time() + 30
    assert matches_schema(wire, EDITS_ONLY_SCHEMA)
    frozen = freeze_terms(terms, deadline=deadline)
    if example['id'] == 'fake_repetition_noop':
        # Explicit experimental protocol change: ignored metadata, not a
        # semantic edit or independent model-quality pass.
        assert example['expected'] == 'ignored_metadata_candidate_unchanged'
        candidate, evidence = resolve_edits_only(raw, wire, frozen, deadline=deadline)
        assert candidate == raw and evidence == []
        effective, corrections, term_evidence = validate_edits(raw, candidate, evidence, frozen)
        assert effective == raw and corrections == [] and term_evidence == []
        assert protected_problem(effective, candidate) is False

        class NoopReply:
            def __init__(self):
                self.calls = []

            async def generate(self, request, call_deadline):
                self.calls.append(request.purpose)
                return LlmResponse(parsed=deepcopy(wire), provider='stub', model_id='noop-protocol')

        provider = NoopReply()
        result = await RefinerService(provider, automatic_retries=0, edits_only_candidate=True).refine(
            raw, frozen, deadline=deadline)
        assert result.text == raw and result.review_required is False
        assert provider.calls == ['refine']  # No hidden audit/retry/correction call.
        assert example == original
        return
    # Structurally valid wrong-kind/literal modifications still fail before audit.
    with pytest.raises(ValueError):
        candidate, evidence = resolve_edits_only(raw, wire, frozen, deadline=deadline)
        validate_edits(raw, candidate, evidence, frozen)
    assert example == original
