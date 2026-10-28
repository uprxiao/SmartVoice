"""A configurable local provider must keep R2 body and classify JSON distinct."""
import asyncio
import json

import httpx
import pytest

from app.core.clock import loop_time
from app.providers.llm.base import LlmRequest
from app.providers.llm.ollama import OllamaProvider


TEXT = '讨论完再改。\n\n1. 保留按钮。\n2. 间距12像素。\n\n最后给我计划🙂。'


def text_request(purpose='refine'):
    return LlmRequest(purpose, '虚构正文整理', {'transcript': TEXT, 'confirmed_terms': ['React']},
        {'type': 'string', 'minLength': 1, 'maxLength': 32768},
        output_mode='text', max_output_tokens=8192)


async def fixture_provider(reply, *, slow=False):
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content))
        if slow:
            await asyncio.sleep(.2)
        return httpx.Response(200, json=reply)
    provider = OllamaProvider('http://fixture.invalid', 'synthetic-local')
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url='http://fixture.invalid', transport=httpx.MockTransport(handler))
    provider.supports_thinking = True
    return provider, seen


async def test_plaintext_is_not_json_and_does_not_force_a_json_format():
    p, seen = await fixture_provider({'done': True, 'done_reason': 'stop',
        'message': {'role': 'assistant', 'content': TEXT}, 'prompt_eval_count': 7, 'eval_count': 19})
    try:
        r = await p.generate(text_request(), loop_time()+1)
        assert r.error is None and r.parsed is None and r.text == TEXT
        assert (r.prompt_tokens, r.completion_tokens) == (7, 19)
        assert 'format' not in seen[0] and seen[0]['think'] is False
        assert seen[0]['options']['num_predict'] == 8192 and seen[0]['stream'] is False
        assert json.loads(seen[0]['messages'][1]['content']) == text_request().input
    finally:
        await p.close()


@pytest.mark.parametrize('changes,code', [
    ({'done': False}, 'invalid_output'),
    ({'done': 1}, 'invalid_output'),
    ({'done_reason': None}, 'invalid_output'),
    ({'done_reason': 'length'}, 'truncated'),
    ({'done_reason': 'refusal'}, 'refused'),
    ({'message': {'content': ' \n '}}, 'invalid_output'),
    ({'message': {'content': ['wrong type']}}, 'invalid_output'),
    ({'message': {'content': 'x'*32769}}, 'invalid_output'),
    ({'message': {'content': TEXT, 'thinking': 'unexpected thought'}}, 'invalid_output'),
    ({'message': {'content': TEXT, 'tool_calls': [{'function': {'name': 'other'}}]}}, 'invalid_output'),
])
async def test_nonfinal_or_wrong_body_never_leaks_partial_text(changes, code):
    p, _ = await fixture_provider({'done': True, 'done_reason': 'stop', 'message': {'content': TEXT}} | changes)
    try:
        r = await p.generate(text_request(), loop_time()+1)
        assert r.error is not None and r.error.code == code
        assert r.text is None and r.parsed is None
    finally:
        await p.close()


async def test_text_deadline_is_absolute_and_classify_does_not_dispatch_text():
    p, seen = await fixture_provider({'done': True, 'done_reason': 'stop', 'message': {'content': TEXT}}, slow=True)
    try:
        r = await p.generate(text_request('classify'), loop_time()+1)
        assert r.error and r.error.code == 'invalid_input' and not seen
        start = loop_time()
        r = await p.generate(text_request(), start+.02)
        assert r.error and r.error.code == 'timeout' and r.text is None
        assert loop_time()-start < .15 and len(seen) == 1
    finally:
        await p.close()


async def test_classification_keeps_the_json_wire_contract():
    schema = {'type': 'object', 'required': ['decision'], 'properties': {'decision': {'type': 'string'}}}
    p, seen = await fixture_provider({'done': True, 'done_reason': 'stop',
        'message': {'content': '{"decision":"ignore"}'}})
    try:
        r = await p.generate(LlmRequest('classify', '虚构分类', {'before': '钮', 'after': '键'}, schema), loop_time()+1)
        assert r.error is None and r.parsed == {'decision': 'ignore'} and r.text is None
        assert seen[0]['format'] == schema
    finally:
        await p.close()
