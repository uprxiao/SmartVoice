"""Repetition proof boundaries; controlled responses, not model quality acceptance."""
from copy import deepcopy

import pytest

from app.providers.llm.base import LlmResponse
from app.services.refine_evidence import validate_edits
from app.services.refiner import RefinerService

REP = '归档收据，归档收据'
RAW = '🙂' + REP + '，不要删除原件。'
CANDIDATE = '🙂归档收据，不要删除原件。'
PROOF = {'kind': 'repetition', 'source_start': 1, 'source_end': 10,
         'source_text': REP, 'replacement_text': '归档收据'}
PATHS = [False, True]
PATH_IDS = ['numeric-legacy', 'exact-production']


@pytest.fixture(scope='session', autouse=True)
def require_owned_database(test_database):
    # Retain the original conftest-owned isolated DB/normal teardown boundary.
    yield


class Answers:
    def __init__(self, wire, audit):
        self.wire, self.audit, self.calls = deepcopy(wire), deepcopy(audit), []

    async def generate(self, request, deadline):
        self.calls.append((deepcopy(request), deadline))
        assert len(self.calls) <= 2, 'unexpected retry or extra model call'
        assert request.purpose in ('refine', 'audit')
        return LlmResponse(parsed=deepcopy(self.wire if request.purpose == 'refine' else self.audit),
                           provider='stub', model_id='repetition-proof-fixture')


def wire(exact, raw, candidate, proofs):
    if exact:
        return {'candidate_text': candidate, 'edits': [
            {'kind': proof['kind'], 'before': proof['source_text'], 'after': proof['replacement_text']}
            for proof in proofs]}
    return {'candidate_text': candidate, 'edits': deepcopy(proofs)}


async def refine(exact, raw, candidate, proofs, audit):
    provider = Answers(wire(exact, raw, candidate, proofs), audit)
    result = await RefinerService(provider, automatic_retries=0, edits_only_candidate=exact).refine(raw)
    return result, provider


def test_default_three_values_and_opt_in_validated_codepoint_proof():
    source = deepcopy(PROOF)
    legacy = validate_edits(RAW, CANDIDATE, [source], [])
    effective, corrections, terms = legacy  # Existing three-value callers stay valid.
    assert legacy == (CANDIDATE, [], [])
    extended = validate_edits(RAW, CANDIDATE, [source], [], include_repetitions=True)
    assert extended == (effective, corrections, terms, [PROOF])
    assert extended[3][0] is not source and source == PROOF
    assert RAW[PROOF['source_start']:PROOF['source_end']] == REP
    assert validate_edits('请检查收据。', '请检查收据。', [], [],
                          include_repetitions=True) == ('请检查收据。', [], [], [])


def test_later_invalid_edit_does_not_return_partial_proof():
    # A valid first proof cannot authorize an invalid later original-string span.
    invalid = {'kind': 'format', 'source_start': len(RAW), 'source_end': len(RAW)+1,
               'source_text': '外', 'replacement_text': '外。'}
    edits = [deepcopy(PROOF), invalid]
    before = deepcopy(edits)
    with pytest.raises(ValueError):
        validate_edits(RAW, CANDIDATE, edits, [], include_repetitions=True)
    assert edits == before


@pytest.mark.parametrize('exact', PATHS, ids=PATH_IDS)
@pytest.mark.parametrize('decision', ['pass', 'reject'], ids=['audit-pass', 'audit-reject'])
@pytest.mark.asyncio
async def test_validated_repetition_has_no_second_model_decision(exact, decision):
    violations = [] if decision == 'pass' else [
        {'category': 'fact', 'source_start': 1, 'source_end': 5, 'reason_code': 'MEANING_NOT_PRESERVED'}]
    result, provider = await refine(exact, RAW, CANDIDATE, [PROOF],
                                   {'decision': decision, 'violations': violations})
    assert [request.purpose for request, _ in provider.calls] == ['refine']
    assert result.text == CANDIDATE and not result.review_required
    _, corrections, terms, repetitions = validate_edits(RAW, CANDIDATE, [PROOF], [], include_repetitions=True)
    assert corrections == terms == [] and repetitions == [PROOF]



@pytest.mark.parametrize('exact', PATHS, ids=PATH_IDS)
@pytest.mark.asyncio
async def test_filler_cleanup_uses_one_generation_without_audit(exact):
    raw, candidate = '嗯，检查收据。', '检查收据。'
    proof = {'kind': 'filler', 'source_start': 0, 'source_end': 2,
             'source_text': '嗯，', 'replacement_text': ''}
    result, provider = await refine(exact, raw, candidate, [proof], {'decision': 'pass', 'violations': []})
    assert result.text == candidate and result.review_required is False and result.review_reason is None
    assert [request.purpose for request, _ in provider.calls] == ['refine']



@pytest.mark.parametrize('exact', PATHS, ids=PATH_IDS)
@pytest.mark.parametrize('fault', ['invalid-replacement', 'overlap', 'protected-candidate'])
@pytest.mark.asyncio
async def test_invalid_repetition_never_reaches_audit(exact, fault):
    proofs, candidate = [deepcopy(PROOF)], CANDIDATE
    if fault == 'invalid-replacement':
        proofs[0]['replacement_text'] = '销毁收据'
        candidate = '🙂销毁收据，不要删除原件。'
    elif fault == 'overlap':
        proofs.append(deepcopy(PROOF))
    else:
        # Valid compression evidence must not license changing another clause.
        candidate = '🙂归档收据，删除原件。'
    result, provider = await refine(exact, RAW, candidate, proofs, {'decision': 'pass', 'violations': []})
    assert [request.purpose for request, _ in provider.calls] == ['refine']
    assert result.text == RAW and result.review_required is True
    assert result.review_reason == ('protected_content_changed' if fault == 'protected-candidate' else 'llm_invalid_output')
