"""A not-ready service must deny WS before upgrade with the HTTP error envelope."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.http_guard import HttpGuard


@pytest.mark.parametrize('ready,fenced', [(False, False), (True, True)])
def test_readiness_denial_has_json_error_and_request_identity(ready, fenced):
    async def run():
        sent = []
        async def application(*_):
            raise AssertionError('not-ready request reached the application')
        async def receive():
            return {'type': 'websocket.connect'}
        async def send(message):
            sent.append(message)
        owner = SimpleNamespace(state=SimpleNamespace(ready=ready, instance_fenced=fenced))
        scope = {'type': 'websocket', 'path': '/ws/v1/sessions/fixture/audio',
                 'headers': [(b'x-request-id', b'root-readiness-control')],
                 'extensions': {'websocket.http.response': {}}}
        await HttpGuard(application, owner)(scope, receive, send)
        assert [item['type'] for item in sent] == ['websocket.http.response.start', 'websocket.http.response.body']
        start, response = sent
        assert start['status'] == 503
        assert dict(start['headers'])[b'content-type'].startswith(b'application/json')
        error = json.loads(response['body'])['error']
        assert error == {'code': 'SERVICE_UNAVAILABLE', 'message': '语音服务尚未就绪',
                         'request_id': 'root-readiness-control', 'retryable': True, 'details': {}}
        assert dict(start['headers'])[b'x-request-id'] == b'root-readiness-control'
        assert not response.get('more_body', False)
    asyncio.run(run())
