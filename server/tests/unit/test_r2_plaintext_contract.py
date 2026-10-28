"""R2 contract controls use fictional data and a local HTTP transport only."""
import asyncio
import hashlib
import json

import httpx
import pytest

from app.core.clock import loop_time
from app.providers.llm.base import LlmRequest, LlmResponse
from app.providers.llm.openai_compatible import OpenAICompatibleProvider
from app.services.llm_validation import validate_response
from app.services.refiner import RefinerService


RAW = '背景：先讨论，不改程序。第一个按钮保持蓝色，第二个间距12像素。最后给我计划。'
TEXT = '背景：先讨论，不改程序。\n\n1. 按钮保持蓝色。\n2. 间距12像素。\n\n最后给我计划。'
TEXT_SCHEMA = {'type': 'string', 'minLength': 1, 'maxLength': 32768}


def request():
    return LlmRequest(purpose='refine', system_prompt='虚构整理',
        input={'transcript': RAW, 'confirmed_terms': ['React']}, schema=TEXT_SCHEMA,
        output_mode='text', max_output_tokens=8192)


def event(value):
    return 'data: ' + json.dumps(value, ensure_ascii=False) + '\r\n\r\n'


def delta(text=None, *, finish=None, **extra):
    d = extra | ({'content': text} if text is not None else {})
    return event({'choices': [{'index': 0, 'delta': d, 'finish_reason': finish}]})


def stream(text=TEXT):
    return (delta(role='assistant') + delta(text[:9]) + delta(text[9:]) + delta(finish='stop')
        + event({'choices': [], 'usage': {'prompt_tokens': 19, 'completion_tokens': 23}})
        + 'data: [DONE]\r\n\r\n')


class Pieces(httpx.AsyncByteStream):
    def __init__(self, value):
        self.value = value.encode()

    async def __aiter__(self):
        # Deliberately split inside UTF-8 characters, CRLF and JSON tokens.
        for pos in range(0, len(self.value), 7):
            yield self.value[pos:pos+7]


async def provider_for(payload, status=200):
    seen = []
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(status, headers={'content-type': 'text/event-stream'}, stream=Pieces(payload))
    p = OpenAICompatibleProvider('https://api.deepseek.com', 'deepseek-flash', 'fictional-test-key', thinking='disabled')
    await p.client.aclose()
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p, seen


async def test_complete_stream_preserves_body_and_has_exact_r2_wire_modes():
    p, seen = await provider_for(stream())
    try:
        r = await p.generate(request(), loop_time()+2)
        assert r.error is None and r.text == TEXT and r.parsed is None
        assert (r.prompt_tokens, r.completion_tokens) == (19, 23)
        wire = seen[0]
        assert wire['thinking'] == {'type': 'disabled'}
        assert wire['stream'] and wire['stream_options'] == {'include_usage': True}
        assert wire['max_tokens'] == 8192 and wire['temperature'] == 0
        assert 'response_format' not in wire and len(wire['messages']) == 2
        assert json.loads(wire['messages'][1]['content']) == request().input
    finally:
        await p.close()


@pytest.mark.parametrize('payload,code', [
    (stream(' \n '), 'invalid_output'),
    (delta(TEXT), 'invalid_output'),
    (delta(TEXT)+delta(finish='stop'), 'invalid_output'),
    (delta(TEXT)+'data: [DONE]\n\n', 'invalid_output'),
    (delta(TEXT)+delta(finish='length')+'data: [DONE]\n\n', 'truncated'),
    (delta(refusal='cannot')+'data: [DONE]\n\n', 'refused'),
    (delta(reasoning_content='hidden thought')+stream(), 'invalid_output'),
    ('data: {broken}\n\n', 'invalid_output'),
    (delta(TEXT)+delta(finish='stop')+delta('extra')+'data: [DONE]\n\n', 'invalid_output'),
    (delta(['wrong type'])+delta(finish='stop')+'data: [DONE]\n\n', 'invalid_output'),
    (stream('x'*32769), 'invalid_output'),
])
async def test_incomplete_or_wrong_stream_never_exposes_partial_body(payload, code):
    p, _ = await provider_for(payload)
    try:
        r = await p.generate(request(), loop_time()+2)
        assert r.error and r.error.code == code
        assert r.text is None and r.parsed is None
    finally:
        await p.close()


async def test_classification_stays_json_without_streaming():
    seen = []
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"decision":"ignore"}'}, 'finish_reason': 'stop'}]})
    p = OpenAICompatibleProvider('http://127.0.0.1:19999', 'fictional-local', None)
    await p.client.aclose()
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        req = LlmRequest('classify', 'JSON only', {'before': '钮', 'after': '键'}, {'type': 'object'})
        r = await p.generate(req, loop_time()+2)
        assert r.parsed == {'decision': 'ignore'} and r.text is None
        assert seen[0]['response_format'] == {'type': 'json_object'}
        assert 'stream' not in seen[0]
    finally:
        await p.close()


@pytest.mark.parametrize('text,parsed,valid', [(TEXT,None,True), (' \n ',None,False), (TEXT,{},False), (None,{},False)])
def test_text_and_json_are_distinct_contracts(text, parsed, valid):
    r = LlmResponse(parsed, 'fixture', 'fixture', text=text)
    assert (validate_response(r, TEXT_SCHEMA, output_mode='text').error is None) is valid


class FullText:
    def __init__(self, values):
        self.values, self.requests = iter(values), []

    async def generate(self, req, deadline):
        self.requests.append(req)
        return LlmResponse(None, 'fixture', 'fixture', text=next(self.values))


async def test_r2_prompt_hash_raw_and_one_call_no_edit_evidence():
    llm = FullText([TEXT])
    s = RefinerService(llm, protocol='ds_r2')
    r = await s.refine(RAW, [{'id': 't1', 'term': 'React', 'norm': 'react', 'aliases': ['reactt']}])
    assert r.text == TEXT and not r.review_required
    assert len(llm.requests) == 1
    req = llm.requests[0]
    assert hashlib.sha256(req.system_prompt.encode()).hexdigest() == 'a1efa73302779e50d7d80396e3020470c4f0b9c522f794e64cf78ef3fee386f8'
    assert req.input == {'transcript': RAW, 'confirmed_terms': ['React']}
    assert req.output_mode == 'text' and req.max_output_tokens == 8192


async def test_r2_invalid_retries_original_only_then_falls_back():
    llm = FullText([' ', ''])
    r = await RefinerService(llm, protocol='ds_r2').refine(RAW)
    assert r.text == RAW and r.review_required and r.review_reason == 'llm_invalid_output'
    assert len(llm.requests) == 2 and all(req.input['transcript'] == RAW for req in llm.requests)


async def test_stream_deadline_is_absolute_even_when_events_keep_arriving():
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b': heartbeat\n\n'
                await asyncio.sleep(.01)
    p = OpenAICompatibleProvider('https://api.deepseek.com', 'deepseek-flash', None, thinking='disabled')
    await p.client.aclose()
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200,stream=Slow())))
    try:
        started = loop_time()
        r = await p.generate(request(), started+.05)
        assert r.error and r.error.code == 'timeout' and r.text is None
        assert loop_time() - started < .5
    finally:
        await p.close()


@pytest.mark.parametrize('payload,ready',[(stream('ready'),True),
    (delta('ready')+delta(finish='stop'),False),(stream('other'),False)])
async def test_text_prepare_proves_a_complete_synthetic_generation(payload,ready):
    from app.providers.asr.base import ProviderError
    p,seen=await provider_for(payload)
    prior=p.client
    def handler(req):
        if req.method=='GET':return httpx.Response(200,json={'data':[{'id':'deepseek-flash'}]})
        seen.append(json.loads(req.content))
        return httpx.Response(200,stream=Pieces(payload))
    await prior.aclose()
    p.client=httpx.AsyncClient(transport=httpx.MockTransport(handler));p.text_probe=True
    try:
        if ready:await p.prepare()
        else:
            with pytest.raises(ProviderError,match='synthetic'):await p.prepare()
        assert len(seen)==1 and seen[0]['max_tokens']==32
        assert json.loads(seen[0]['messages'][1]['content'])=={'transcript':'ready','confirmed_terms':[]}
    finally:await p.close()
