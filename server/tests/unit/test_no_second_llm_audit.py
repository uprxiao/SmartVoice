"""User-confirmed 2026-10-19 contract: generate once, retain local checks."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.core.config import LlmConfig
from app.providers.llm.base import LlmResponse
from app.services.refiner import RefinerService


class OnlyRefine:
    def __init__(self, response):
        self.response, self.calls = response, []

    async def generate(self, request, deadline):
        self.calls.append(deepcopy(request))
        assert request.purpose == 'refine', 'a second LLM audit is no longer allowed'
        return LlmResponse(parsed=deepcopy(self.response), provider='stub', model_id='no-audit-contract')


def payload(exact, raw, candidate, spans):
    if exact:
        edits = [dict(kind=p['kind'], before=p['source_text'], after=p['replacement_text'],
                      **{k: p[k] for k in ('dictionary_term_id', 'dictionary_alias') if k in p})
                 for p in spans]
    else:
        edits = deepcopy(spans)
    return {'candidate_text': candidate, 'edits': edits}


@pytest.mark.parametrize('exact', [False, True], ids=['numeric', 'production-exact'])
@pytest.mark.asyncio
async def test_valid_filler_cleanup_has_no_second_llm_call(exact):
    raw, candidate = '嗯，检查日志，不要删除配置。', '检查日志，不要删除配置。'
    proof = {'kind':'filler','source_start':0,'source_end':2,'source_text':'嗯，','replacement_text':''}
    llm = OnlyRefine(payload(exact, raw, candidate, [proof]))
    result = await RefinerService(llm, edits_only_candidate=exact).refine(raw)
    assert (result.text, result.review_required, result.review_reason) == (candidate, False, None)
    assert [r.purpose for r in llm.calls] == ['refine']


@pytest.mark.parametrize('exact', [False, True], ids=['numeric', 'production-exact'])
@pytest.mark.asyncio
async def test_both_alias_positions_can_change_without_semantic_disambiguation(exact):
    raw = '启动我的助手 cloud 来检查代码，再比较 cloud 云服务的价格'
    candidate = raw.replace('cloud', 'claw')
    term = {'id':'00000000-0000-0000-0000-000000000001','term':'claw','norm':'claw',
            'aliases':['cloud'],'ambiguous_alias_norms':[],'contexts':[]}
    spans=[]
    for start in [raw.index('cloud'), raw.rindex('cloud')]:
        spans.append({'kind':'term_correction','source_start':start,'source_end':start+5,
                      'source_text':'cloud','replacement_text':'claw',
                      'dictionary_term_id':term['id'],'dictionary_term':'claw','dictionary_alias':'cloud'})
    response=payload(exact, raw, candidate, spans)
    if exact:
        # Each quote still identifies one original position unambiguously.
        response['edits'][0].update(before='助手 cloud',after='助手 claw')
        response['edits'][1].update(before='比较 cloud',after='比较 claw')
    llm=OnlyRefine(response)
    result=await RefinerService(llm, edits_only_candidate=exact).refine(raw,[term])
    assert (result.text,result.review_required,result.review_reason)==(candidate,False,None)
    assert [r.purpose for r in llm.calls]==['refine']


@pytest.mark.parametrize('exact', [False, True], ids=['numeric', 'production-exact'])
@pytest.mark.asyncio
async def test_removing_audit_does_not_remove_number_and_negation_guards(exact):
    raw='等待3.5秒，不要删除配置。'
    llm=OnlyRefine(payload(exact,raw,'等待35秒，删除配置。',[]))
    result=await RefinerService(llm, edits_only_candidate=exact).refine(raw)
    assert result.text==raw and result.review_required
    assert result.review_reason=='protected_content_changed'
    assert [r.purpose for r in llm.calls]==['refine']


def test_configuration_cannot_claim_an_enabled_model_audit():
    assert LlmConfig().fidelity_audit=='off'
    with pytest.raises(ValidationError):
        LlmConfig(fidelity_audit='conditional')
