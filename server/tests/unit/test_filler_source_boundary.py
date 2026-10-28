"""A filler exemption cannot delete a prefix/suffix inside an ordinary word."""
from copy import deepcopy
import pytest
from app.providers.llm.base import LlmResponse
from app.services.refiner import RefinerService


class Edit:
    def __init__(self, raw, before, exact):
        self.raw, self.before, self.exact, self.calls = raw, before, exact, []

    async def generate(self, request, deadline):
        self.calls.append(request.purpose)
        candidate = self.raw.replace(self.before, '', 1)
        if self.exact:
            edits = [{'kind':'filler', 'before':self.before, 'after':''}]
        else:
            start = self.raw.index(self.before)
            edits = [{'kind':'filler', 'source_start':start, 'source_end':start+len(self.before),
                      'source_text':self.before, 'replacement_text':''}]
        value = {'edits':edits, 'candidate_text':candidate}
        return LlmResponse(parsed=deepcopy(value), provider='stub', model_id='filler-boundary-fixture')


@pytest.mark.parametrize('exact', [False, True], ids=['numeric','production-exact'])
@pytest.mark.parametrize('raw,before', [
    ('那个方案先别动，先检查另一个方案。','那个'),
    ('那个人正在检查日志，不要打断。','那个'),
    ('额外检查两次，不要删除日志。','额'),
    ('先检查日志。那个方案先别动。','那个'),
    ('检查结束，额外保留一份副本。','额'),
    ('检查可用余额，其他保持原样。','额'),
    ('记录夏季 summer 的配置，保持原样。','um'),
    ('保留名称 uhura，其他保持原样。','uh'),
    ('客户 customer 数据保持原样。','er'),
])
@pytest.mark.asyncio
async def test_filler_cannot_delete_a_piece_of_an_original_word(raw, before, exact):
    p = Edit(raw,before,exact)
    r = await RefinerService(p,edits_only_candidate=exact).refine(raw)
    assert r.text == raw and r.review_required and r.review_reason == 'llm_invalid_output'
    assert p.calls == ['refine']


@pytest.mark.parametrize('exact', [False, True], ids=['numeric','production-exact'])
@pytest.mark.parametrize('raw,before', [
    ('那个，先检查日志。','那个，'),
    ('嗯那个，先检查日志。','嗯那个，'),
    ('嗯那个，先检查日志。','嗯那个'),
    ('额，先检查日志。','额，'),
    ('呃，先检查日志。','呃，'),
    ('嗯先检查日志。','嗯'),
    ('那个 先检查日志。','那个 '),
    ('um, check the logs.','um,'),
])
@pytest.mark.asyncio
async def test_standalone_filler_and_vocal_prefix_still_clean_up(raw, before, exact):
    p = Edit(raw,before,exact)
    r = await RefinerService(p,edits_only_candidate=exact).refine(raw)
    assert r.text == raw.replace(before,'',1) and not r.review_required
    assert p.calls == ['refine']
