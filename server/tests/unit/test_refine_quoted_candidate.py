"""Quoted literal invariants apply to the actual candidate, not just declared edits."""
import pytest
from app.providers.llm.base import LlmResponse
from app.services.refiner import RefinerService


class Wire:
    def __init__(self, exact, candidate):
        self.exact, self.candidate, self.calls = exact, candidate, []

    async def generate(self, request, deadline):
        self.calls.append(request.purpose)
        return LlmResponse(parsed={'candidate_text': self.candidate, 'edits': []},
                           provider='stub', model_id='quoted-candidate-fixture')


@pytest.mark.parametrize('exact', [False, True], ids=['numeric', 'production-exact'])
@pytest.mark.parametrize('raw,candidate', [
    ('请保留“嗯那个”，不要改动。', '请保留“那个”，不要改动。'),
    ('显示“暂缓出货”，不改变其他内容。', '显示“立刻出货”，不改变其他内容。'),
    ('原样写入"客户确认"，其他照旧。', '原样写入"客户撤销"，其他照旧。'),
    ('提示文字是`保持原样`，不要删除日志。', '提示文字是`改成其他`，不要删除日志。'),
    ('保留“甲”和“乙”，不要合并。', '保留“甲乙”，不要合并。'),
    ('引用“同意”，再次引用“同意”，不省略。', '引用“同意”，再次引用同意，不省略。'),
    ('请保留‘嗯那个’，不要改动。', '请保留‘那个’，不要改动。'),
])
@pytest.mark.asyncio
async def test_free_candidate_cannot_change_quoted_literals_without_declared_edits(raw, candidate, exact):
    provider = Wire(exact, candidate)
    result = await RefinerService(provider, edits_only_candidate=exact).refine(raw)
    assert result.text == raw and result.review_required and result.review_reason == 'protected_content_changed'
    assert provider.calls == ['refine']


@pytest.mark.parametrize('exact', [False, True], ids=['numeric', 'production-exact'])
@pytest.mark.parametrize('raw', [
    '对“确认”“确认”分别保留，其他不变。',
    '请逐字写“嗯，那个，先别动”。',
    '原文为`然后然后`，不要整理。',
    '请照写‘嗯，那个’，不要改动。',
    '引文保留“客户确认”，额外保存一份日志。',
])
@pytest.mark.asyncio
async def test_correct_quoted_literals_do_not_trigger_unprocessed_filler_or_repeat(raw, exact):
    provider = Wire(exact, raw)
    result = await RefinerService(provider, edits_only_candidate=exact).refine(raw)
    assert result.text == raw and not result.review_required and result.review_reason is None
    assert provider.calls == ['refine']


@pytest.mark.parametrize('exact', [False, True], ids=['numeric', 'production-exact'])
@pytest.mark.parametrize('raw,candidate', [
    ('展示客户确认这四个字，不要修改其他内容。', '展示“客户确认”这四个字，不要修改其他内容。'),
    ('保留“甲”，再展示乙。', '保留“甲”，再展示“乙”。'),
])
@pytest.mark.asyncio
async def test_new_quote_formatting_can_preserve_all_original_literals(raw, candidate, exact):
    provider = Wire(exact, candidate)
    result = await RefinerService(provider, edits_only_candidate=exact).refine(raw)
    assert result.text == candidate and not result.review_required and result.review_reason is None
    assert provider.calls == ['refine']
