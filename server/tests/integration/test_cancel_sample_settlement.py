"""BR10 cancellation keeps every accepted sample, including the sub-second tail."""
from __future__ import annotations
import asyncio
import json
import struct
import pytest
import httpx
from tests.conftest import auth_headers, register_and_login
from tests.test_sessions import create_session, frame
from tests.cases.test_dict_cases import fetch_one
from tests.integration.test_voice_cancel_boundaries import create, facts, settle
from tests.integration.test_normal_shutdown_contract import WsProbe
from app.services.session_runtime import ProtocolViolation, stats_service

@pytest.mark.parametrize('cancel_path',['http','websocket'])
def test_cancel_settles_subsecond_accepted_audio(app_client, cancel_path, record_property):
    pair=register_and_login(app_client,email='cancel-samples@example.invalid')
    headers=auth_headers(pair);owner=pair['user']['id']
    sid=create_session(app_client,headers,'cancel-samples-'+cancel_path).json()['session_id']
    with app_client.websocket_connect(f'/ws/v1/sessions/{sid}/audio',headers=headers) as ws:
        assert ws.receive_json()['type']=='ready'
        for seq in range(2):
            ws.send_bytes(frame(seq));ack=ws.receive_json()
            assert ack['type']=='audio.ack' and ack['total_samples']==(seq+1)*4000
        if cancel_path=='http':
            response=app_client.post(f'/api/v1/sessions/{sid}/cancel',headers=headers)
            assert response.status_code==200
        else:
            ws.send_json({'type':'cancel'})
            while ws.receive_json()['type']!='cancelled':pass
        detail=app_client.get(f'/api/v1/sessions/{sid}',headers=headers).json()
        assert detail['state']=='cancelled'
        assert detail['received_samples']==8000
    assert fetch_one('select received_samples from session_contributions where user_id=$1 and session_id=$2',owner,sid)==8000
    assert fetch_one('select received_audio_samples from usage_daily where user_id=$1',owner)==8000
    again=app_client.post(f'/api/v1/sessions/{sid}/cancel',headers=headers)
    assert again.status_code==200 and again.json()['received_samples']==8000
    assert fetch_one('select received_audio_samples from usage_daily where user_id=$1',owner)==8000
    record_property('cancel_samples',json.dumps({'path':cancel_path,'samples':8000,'milliseconds':500,'repeat_unchanged':True}))

@pytest.mark.parametrize('outcome',['commit','rollback'])
def test_cancel_pending_commit_fences_new_audio_and_cleans_on_error(app_client,monkeypatch,record_property,outcome):
    pair=register_and_login(app_client,email='cancel-pending@example.invalid')
    headers=auth_headers(pair);owner=pair['user']['id']
    async def scenario():
        app=app_client.app;manager=app.state.sessions;wire=None;request=None
        entered=asyncio.Event();release=asyncio.Event();observed=asyncio.Event();trace=[]
        original_stats=stats_service.record_session;original_accept=manager._accept_frame
        sid=None
        async def gated_stats(db,session,state,*args,**kwargs):
            if session.id==sid and state=='cancelled':
                entered.set();await release.wait()
                if outcome=='rollback':raise RuntimeError('OWNED_CANCEL_TRANSACTION_FAILURE')
            return await original_stats(db,session,state,*args,**kwargs)
        async def accept(runtime,packet):
            seq=struct.unpack_from('<I',packet)[0]
            try:
                result=await original_accept(runtime,packet)
                if runtime.session_id==sid and seq==1:trace.append('accepted');observed.set()
                return result
            except ProtocolViolation as error:
                if runtime.session_id==sid and seq==1:trace.append(error.code);observed.set()
                raise
        monkeypatch.setattr(stats_service,'record_session',gated_stats);monkeypatch.setattr(manager,'_accept_frame',accept)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://testserver') as http:
            sid=await create(http,headers,'cancel-pending-'+outcome);runtime=manager.runtimes[sid];audio=runtime.audio;stream=runtime.asr
            try:
                wire=WsProbe(app,headers,sid);await wire.start();assert runtime.samples==4000
                wire.drain_task=asyncio.create_task(wire.drain())
                request=asyncio.create_task(http.post(f'/api/v1/sessions/{sid}/cancel',headers=headers))
                async with asyncio.timeout(3):await entered.wait()
                await wire.send({'type':'websocket.receive','bytes':struct.pack('<II',1,4000)+struct.pack('<h',101)*4000})
                async with asyncio.timeout(3):await observed.wait()
                assert trace==['AUDIO_PROTOCOL_ERROR'] and runtime.samples==stream.samples==4000
                assert not any(e['type']=='audio.ack' and e['seq']==1 for e in wire.events)
                release.set()
                response=await request
                assert response.status_code==(200 if outcome=='commit'else 500)
                async with asyncio.timeout(5):
                    while runtime.audio or not stream.cancelled:await asyncio.sleep(.01)
                assert runtime.audio is audio and len(audio)==0 and stream.cancelled and runtime.asr is None
                async with asyncio.timeout(5):
                    while True:
                        value=await facts(app,owner,sid)
                        if value['session']['state']in('cancelled','failed'):break
                        await asyncio.sleep(.01)
                assert value['session']['state']==('cancelled'if outcome=='commit'else'failed')
                assert value['counts']['results']==value['counts']['insertions']==value['counts']['learning_events']==0
                return {'outcome':outcome,'late_frame':trace,'samples':runtime.samples,'pcm_after':len(audio),'http':response.status_code,'terminal':value['session']['state']}
            finally:
                release.set()
                if request and not request.done():await asyncio.gather(request,return_exceptions=True)
                await settle(wire)
    record_property('cancel_pending',json.dumps(app_client.portal.call(scenario),sort_keys=True))
