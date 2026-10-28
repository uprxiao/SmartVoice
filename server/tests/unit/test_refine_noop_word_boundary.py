"""A correct unchanged response must not be rejected for a Chinese word prefix."""
from copy import deepcopy
import pytest
from app.providers.llm.base import LlmResponse
from app.services.refiner import RefinerService


class KeepOriginal:
    def __init__(self, exact, raw):
        self.exact, self.raw, self.calls = exact, raw, []

    async def generate(self, request, deadline):
        self.calls.append(request.purpose)
        response = {'edits': []} if self.exact else {'edits': [], 'candidate_text': self.raw}
        return LlmResponse(parsed=deepcopy(response), provider='stub', model_id='unchanged-word-fixture')


@pytest.mark.parametrize('exact', [False, True], ids=['numeric', 'production-exact'])
@pytest.mark.parametrize('raw', [
    '那个方案先别动，先检查另一个方案。',
    '那个人正在检查日志，不要打断。',
    '额外检查两次，不要删除日志。',
    '先检查日志。那个方案先别动。',
    '检查结束，额外保留一份副本。',
])
@pytest.mark.asyncio
async def test_correct_unchanged_word_is_not_invented_filler(raw, exact):
    provider = KeepOriginal(exact, raw)
    result = await RefinerService(provider, edits_only_candidate=exact).refine(raw)
    assert result.text == raw and result.review_required is False and result.review_reason is None
    assert provider.calls == ['refine']


@pytest.mark.parametrize('exact', [False, True], ids=['numeric', 'production-exact'])
@pytest.mark.parametrize('raw', [
    '那个，先检查日志。',
    '嗯那个，先检查日志。',
    '额，先检查日志。',
    '呃。先检查日志。',
    '先检查，然后然后保留日志。',
])
@pytest.mark.asyncio
async def test_obvious_unprocessed_fillers_and_repetitions_still_fail(raw, exact):
    provider = KeepOriginal(exact, raw)
    result = await RefinerService(provider, edits_only_candidate=exact).refine(raw)
    assert result.text == raw and result.review_required and result.review_reason == 'llm_invalid_output'
    assert provider.calls == ['refine']
