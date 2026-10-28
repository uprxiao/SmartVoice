"""A loaded model and an HTTP200 do not prove successful generation."""
import asyncio
import json

import httpx
import pytest

from app.providers.asr.base import ProviderError
from app.providers.llm.ollama import OllamaProvider


VALID={'done':True,'done_reason':'stop','response':'{"ready":true}'}


def provider(reply, *, capabilities=(), on_generate=None):
    requests=[]
    async def handler(request):
        requests.append(request)
        if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'synthetic-local'}]})
        if request.url.path=='/api/show':return httpx.Response(200,json={'capabilities':list(capabilities)})
        assert request.url.path=='/api/generate'
        if on_generate:await on_generate()
        return httpx.Response(200,json=reply)
    p=OllamaProvider('http://fixture.invalid','synthetic-local')
    original=p.client
    p.client=httpx.AsyncClient(base_url='http://fixture.invalid',transport=httpx.MockTransport(handler))
    return p,original,requests


@pytest.mark.asyncio
@pytest.mark.parametrize('capabilities',[(),('thinking','completion')])
async def test_prepare_runs_only_fixed_synthetic_generation(capabilities):
    p,original,requests=provider(VALID,capabilities=capabilities)
    try:
        await p.prepare()
        assert [r.url.path for r in requests]==['/api/tags','/api/show','/api/generate']
        body=json.loads(requests[-1].content)
        assert isinstance(body.get('prompt'),str) and body['prompt']
        assert body['format']=={'type':'object','properties':{'ready':{'type':'boolean'}},
                                'required':['ready'],'additionalProperties':False}
        assert body['options']=={'num_ctx':8192,'temperature':0,'num_predict':32}
        assert body['stream'] is False and body['keep_alive']==-1
        assert ('think' in body)==('thinking' in capabilities)
        if 'think' in body:assert body['think'] is False
        assert 'messages' not in body and 'transcript' not in body and 'terms' not in body
    finally:await p.close();await original.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('reply',[
    {},{'done':True},{'done':True,'done_reason':'load','response':''},
    {'done':False,'done_reason':'stop','response':'{"ready":true}'},
    {'done':1,'done_reason':'stop','response':'{"ready":true}'},
    {**VALID,'done_reason':'length'}, {**VALID,'done_reason':'error'},
    {**VALID,'response':''},{**VALID,'response':'{'},{**VALID,'response':'true'},
    {**VALID,'response':'[]'},{**VALID,'response':'{"ready":false}'},
    {**VALID,'response':'{"ready":1}'},{**VALID,'response':'{"ready":true,"extra":1}'},
    {**VALID,'response':{'ready':True}},[],
])
async def test_load_only_or_invalid_generation_never_becomes_prepared(reply):
    p,original,requests=provider(reply)
    try:
        with pytest.raises(ProviderError) as exc:await p.prepare()
        assert exc.value.code=='unavailable'
        assert sum(r.url.path=='/api/generate' for r in requests)==1
    finally:await p.close();await original.aclose()


@pytest.mark.asyncio
async def test_healthy_generation_does_not_require_optional_usage_metadata():
    p,original,_=provider(VALID)
    try:await p.prepare()
    finally:await p.close();await original.aclose()


@pytest.mark.asyncio
async def test_cancelling_prepare_does_not_hide_or_restart_inference():
    started=asyncio.Event();stopped=asyncio.Event()
    async def hold():
        started.set()
        try:await asyncio.Future()
        finally:stopped.set()
    p,original,requests=provider(VALID,on_generate=hold)
    task=asyncio.create_task(p.prepare())
    try:
        async with asyncio.timeout(1):await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert stopped.is_set() and sum(r.url.path=='/api/generate' for r in requests)==1
    finally:
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        await p.close();await original.aclose()
