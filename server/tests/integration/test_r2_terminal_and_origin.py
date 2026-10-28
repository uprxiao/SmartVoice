"""Existing API10/21 parameters: real ASGI/WS/PG, explicitly Mock inference."""
from __future__ import annotations

import asyncio
import json
import struct

import httpx
import pytest

from tests.conftest import auth_headers, register_and_login
from tests.cases.test_dict_cases import key
from tests.test_sessions import create_session
from tests.ws_assertions import assert_handshake_denied, cancel_and_drain
from tests.integration.test_normal_shutdown_contract import WsProbe
from tests.integration.test_voice_cancel_boundaries import create, facts, settle


@pytest.mark.parametrize('kind',['allowed','forbidden','desktop'])
def test_origin_http_response_preflight_and_websocket_auth_matrix(app_client,kind,record_property):
    allowed='http://127.0.0.1:17864'
    origin=allowed if kind=='allowed' else 'http://untrusted.example' if kind=='forbidden' else None
    extra={'Origin':origin} if origin else {}
    tokens=register_and_login(app_client,email='origin-followup@example.invalid')
    headers=auth_headers(tokens)
    if kind=='allowed':app_client.app.state.config.security.ws_origins=[allowed]
    response=app_client.get('/api/users/me',headers={**headers,**extra})
    assert response.status_code==200
    assert response.headers.get('access-control-allow-origin')==(allowed if kind=='allowed' else None)
    assert not response.headers.get('access-control-allow-credentials')
    unauth=app_client.get('/api/users/me',headers=extra)
    assert unauth.status_code==401 and unauth.json()['error']['code']=='AUTH_INVALID'
    if origin:
        pre=app_client.options('/api/auth/login',headers={**extra,
            'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'})
        assert pre.status_code==(200 if kind=='allowed' else 400)
        assert pre.headers.get('access-control-allow-origin')==(allowed if kind=='allowed' else None)
        assert not pre.headers.get('access-control-allow-credentials')
    session=create_session(app_client,headers,key()).json()
    path='/ws/v1/sessions/'+session['session_id']+'/audio'
    assert_handshake_denied(app_client,path,extra,403 if kind=='forbidden' else 401,
        'FORBIDDEN' if kind=='forbidden' else 'AUTH_INVALID')
    if kind=='forbidden':
        assert_handshake_denied(app_client,path,{**headers,**extra},403,'FORBIDDEN')
        current=app_client.get('/api/v1/sessions/'+session['session_id'],headers=headers).json()
        assert current['state']=='created' and current['received_samples']==0 and current['result'] is None
        app_client.post('/api/v1/sessions/'+session['session_id']+'/cancel',headers=headers).raise_for_status()
    else:
        with app_client.websocket_connect(path,headers={**headers,**extra}) as ws:
            assert ws.receive_json()['type']=='ready'
            cancel_and_drain(ws)
    record_property('origin_matrix',json.dumps({'kind':kind,'real_http_ws_pg':True,
        'browser_ui':False,'bearer_enforced':True,'credentials_allowed':False},sort_keys=True))


@pytest.mark.parametrize('violation',['binary-after-end','malformed-json','oversized-json'])
def test_committed_ready_rejects_actual_extra_message_before_socket_close_without_rollback(
    app_client,monkeypatch,record_property,violation,
):
    pair=register_and_login(app_client,email='ready-extra-followup@example.invalid')
    headers,owner=auth_headers(pair),pair['user']['id']
    async def scenario():
        app,manager=app_client.app,app_client.app.state.sessions
        release,ready_sent,fault_seen=asyncio.Event(),asyncio.Event(),asyncio.Event()
        wire=None; trace=[]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as http:
            sid=await create(http,headers,'terminal-followup-'+violation)
            original_fail=manager._fail; runtime=manager.runtimes[sid];audio=runtime.audio
            async def fail(current,code,**kwargs):
                result=await original_fail(current,code,**kwargs)
                if current is runtime:
                    trace.append({'code':code,'result_unchanged':(await facts(app,owner,sid))['counts']['results']==1})
                    fault_seen.set()
                return result
            monkeypatch.setattr(manager,'_fail',fail)
            async def transport(scope,receive,send):
                async def controlled_send(message):
                    await send(message)
                    if message['type']=='websocket.send' and json.loads(message['text'])['type']=='result.ready':
                        ready_sent.set()
                        await release.wait()  # Only hold socket closure after genuine ready delivery.
                await app(scope,receive,controlled_send)
            try:
                wire=WsProbe(transport,headers,sid);await wire.start();await wire.end()
                while (await wire.event())['type']!='result.ready':pass
                await ready_sent.wait()
                before=await facts(app,owner,sid)
                assert before['session']['state']=='ready' and before['counts']['results']==1
                assert before['counts']['insertions']==before['counts']['learning_events']==0
                assert runtime.audio==audio==b'' and runtime.asr is None
                packet=({'type':'websocket.receive','bytes':struct.pack('<II',1,1)+b'\x01\x00'}
                    if violation=='binary-after-end' else {'type':'websocket.receive',
                        'text':'{' if violation=='malformed-json' else 'x'*4097})
                await wire.send(packet)
                async with asyncio.timeout(1):await fault_seen.wait()
                assert await facts(app,owner,sid)==before
                assert trace==[{'code':'MESSAGE_TOO_LARGE' if violation=='oversized-json' else 'AUDIO_PROTOCOL_ERROR',
                    'result_unchanged':True}]
                release.set();await wire.drain()
                assert await facts(app,owner,sid)==before
                assert [e['type'] for e in wire.events].count('result.ready')==1
                assert wire.close_code==(1009 if violation=='oversized-json' else 4400)
                detail=(await http.get('/api/v1/sessions/'+sid,headers=headers)).json()
                assert detail['state']=='ready' and detail['current_result_id']==detail['result']['result_id']
                return {'violation':violation,'real_extra_message_reached_guard_after_commit':True,
                    'ready_events':1,'result_rows':1,'insertions':0,'learning':0,
                    'terminal_and_statistics_unchanged':True,'physical_models_or_native':False}
            finally:
                release.set();await settle(wire)
    record_property('terminal_extra_after_commit',json.dumps(app_client.portal.call(scenario),sort_keys=True))
