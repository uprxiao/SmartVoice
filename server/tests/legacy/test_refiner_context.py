"""Deterministic guard tests; real-model evidence lives in probe_refiner_context."""
import asyncio

import pytest

from smartdd_voice.config import Refiner as Config
from smartdd_voice.refiner import Refiner


def learned(term='claw'):
    return {'term': term, 'aliases': ['cloud'], 'contexts': [
        {'alias': 'cloud', 'left': '请启动 AI 编程助手 ', 'right': '，用它生成代码。'}]}


class RoleRefiner(Refiner):
    def __init__(self):
        super().__init__(Config())
        self.seen = []
        self.decisions = []
        self.decision = 'term'
        self.cleanup = ''

    async def context_kind(self, left, right):
        self.seen.append((left, right))
        if '云服务' in right or 'service' in right:
            return 'computing_concept'
        if '助手' in left:
            return 'named_software'
        return 'unclear'

    async def classify(self, before, after, left, right):
        self.decisions.append((before, after, left, right))
        return self.decision

    async def json(self, *args, **kwargs):
        return {'text': self.cleanup}


def run_with_refiner(check):
    async def scenario():
        refiner = RoleRefiner()
        try:
            await check(refiner)
        finally:
            await refiner.close()
    asyncio.run(scenario())


def test_each_occurrence_uses_its_own_nearest_clause():
    async def check(refiner):
        raw = '请让AI编程助手cloud生成代码，然后保存到cloud云服务。'
        assert await refiner.apply_terms(raw, [learned()]) == '请让AI编程助手claw生成代码，然后保存到cloud云服务。'
        assert ('然后保存到', '云服务') in refiner.seen
        assert not any('生成代码，然后' in left for left, _ in refiner.seen)
        assert refiner.decisions == []
    run_with_refiner(check)


@pytest.mark.parametrize('raw', ['我们用cloud云服务存储图片。', 'The cloud service stores backups.', 'cloud'])
def test_manual_alias_cannot_globally_replace_ordinary_or_unknown_word(raw):
    async def check(refiner):
        assert await refiner.apply_terms(raw, [{'term': 'claw', 'aliases': ['cloud']}]) == raw
        assert refiner.decisions == []
    run_with_refiner(check)


def test_manual_named_alias_still_requires_spelling_correction():
    async def check(refiner):
        raw = '请启动AI助手cloud。'
        term = {'term': 'claw', 'aliases': ['cloud']}
        refiner.decision = 'ignore'
        assert await refiner.apply_terms(raw, [term]) == raw
        refiner.decision = 'term'
        assert await refiner.apply_terms(raw, [term]) == '请启动AI助手claw。'
    run_with_refiner(check)


def test_identical_unknown_context_needs_separate_spelling_check():
    async def check(refiner):
        raw = '本机词库保存到 SQL lite 数据库。'
        term = {'term': 'SQLite', 'aliases': ['SQL lite'], 'contexts': [
            {'alias': 'SQL lite', 'left': '本机词库保存到 ', 'right': ' 数据库。'}]}
        assert await refiner.apply_terms(raw, [term]) == '本机词库保存到 SQLite 数据库。'
        refiner.decision = 'suggest'
        assert await refiner.apply_terms(raw, [term]) == raw
        # Unknown roles in a different sentence cannot borrow the old context.
        other = '请打开 SQL lite。'
        refiner.decision = 'term'
        assert await refiner.apply_terms(other, [term]) == other
    run_with_refiner(check)


def test_competing_private_names_for_same_alias_are_left_unchanged():
    async def check(refiner):
        raw = '请启动AI编程助手cloud。'
        assert await refiner.apply_terms(raw, [learned('claw'), learned('Claude')]) == raw
    run_with_refiner(check)


def test_alias_boundary_does_not_change_embedded_english_words():
    async def check(refiner):
        raw = 'cloudy mycloud cloud_name cloud2 Cloudflare'
        assert await refiner.apply_terms(raw, [learned()]) == raw
        assert refiner.seen == []
    run_with_refiner(check)


@pytest.mark.parametrize('raw,cleanup', [
    ('我们用cloud云服务存储图片。', '我们用云服务存储图片。'),
    ('请启动AI助手cloud。', '请启动AI助手cloud。'),
    ('不要上传42个文件。', '不要上传42个文件。 Added'),
])
def test_cleanup_cannot_remove_revert_or_add_english_words(raw, cleanup):
    async def check(refiner):
        refiner.cleanup = cleanup
        corrected = await refiner.apply_terms(raw, [learned()])
        text, review = await refiner.refine(raw, [learned()])
        assert text == corrected
        assert review and '英文词语' in review
    run_with_refiner(check)


def test_punctuation_cleanup_with_preserved_terms_is_accepted():
    async def check(refiner):
        refiner.cleanup = '请启动 AI 助手 claw，不要上传 42 个文件。'
        text, review = await refiner.refine('请启动AI助手cloud，不要上传42个文件。', [learned()])
        assert text == refiner.cleanup and review is None
    run_with_refiner(check)


def test_role_classifier_receives_masked_target_and_rejects_unknown_category():
    async def scenario():
        refiner = Refiner(Config())
        async def response(system, data, schema, max_tokens):
            assert data == {'left': '请使用AI助手', 'target': '【待判断位置】', 'right': '编程'}
            assert 'claw' not in str(data) and 'cloud' not in str(data)
            return {'kind': 'unvalidated-category'}
        refiner.json = response
        try:
            assert await refiner.context_kind('请使用AI助手', '编程') == 'unclear'
        finally:
            await refiner.close()
    asyncio.run(scenario())
