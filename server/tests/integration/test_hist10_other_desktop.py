"""Prepared HIST10 approved device-policy integration; root alone executes.
Formal API/WS/PG/ScheduledLlmProvider/Refiner, original Mock physical providers.
Only physical-provider failure/return gates and existing admin-role fixture;
no SQL UPDATE of ready/review/raw/result/session, no real model/audio/native claim.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass,field
import hashlib,json,uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.clock import loop_time
from app.providers.asr.base import ProviderError
from app.providers.llm.base import LlmResponse
from app.services.inference_context import current_scope
from tests.conftest import auth_headers,register_and_login
from tests.cases.test_dict_cases import fetch_one,key,login,new_user
from tests.integration.test_capacity_admission_atomicity import AudioFixture,_failed_audio,atomic_client
from tests.integration.test_insertion_device_role_matrix import business_snapshot
from tests.integration.test_normal_shutdown_contract import require_owned_database
from tests.integration.test_retry_postcommit_caller_cancel import wait_until
from tests.test_admin import login_admin
from tests.test_retry import wait_state
from tests.test_sessions import create_session,dictation
from tests.ws_assertions import assert_handshake_denied,cancel_and_drain

RAW='嗯，保留当前文档。'
REFINED='保留当前文档。'
ORIGIN_FIELDS=('id','user_id','device_id','created_login_sid','client_request_id','created_at','end_accepted_at','first_terminal_at','received_samples','audio_complete')

@dataclass
class PhysicalControl:
    owner: str
    calls: list=field(default_factory=list)
    gate_retry: bool=False
    entered: object=None
    release: object=None
    cancelled_while_gated: bool=False
    real_reply_delivered: bool=False
    original_reply: object=None
    gate_deadline: float|None=None


def pair(client):
    require_owned_database()
    email='hist10-device-'+key()+'@example.invalid'
    i1,i2=key(),key();d1=new_user(client,email,i1)
    response=login(client,email,i2);assert response.status_code==200,response.text
    d2=response.json();owner=d1['user']['id'];assert d2['user']['id']==owner
    devices=[fetch_one('SELECT id FROM devices WHERE user_id=$1 AND installation_id=$2',owner,i)for i in(i1,i2)]
    sids=[fetch_one('SELECT id FROM login_sessions WHERE user_id=$1 AND device_id=$2 AND revoked_at IS NULL',owner,d)for d in devices]
    assert all(devices)and devices[0]!=devices[1]and all(sids)and sids[0]!=sids[1]
    return owner,[auth_headers(d1),auth_headers(d2)],devices,sids


def prepare_fallback(client,monkeypatch,owner,headers):
    """Same physical seam as prepare_refine_fallback, without dictionary setup."""
    physical=AudioFixture(client,monkeypatch,{owner:RAW})
    inner=client.app.state.llm.inner;original=inner.generate;control=PhysicalControl(owner)
    failed_once=False
    async def physical_boundary(request,deadline):
        nonlocal failed_once
        scope=current_scope.get();assert scope is not None and scope.user_id==owner
        control.calls.append((scope.attempt_no,request.purpose))
        if not failed_once and scope.attempt_no==1 and request.purpose=='refine':
            failed_once=True
            return LlmResponse(parsed=None,provider=inner.provider_id,model_id=inner.model_id,
                error=ProviderError('unavailable','controlled original physical provider unavailable',retryable=False))
        answer=await original(request,deadline)  # Real original Mock wire reply.
        if control.gate_retry and scope.attempt_no==2 and request.purpose=='refine':
            assert answer.error is None
            control.original_reply=answer;control.gate_deadline=deadline;control.entered.set()
            try:await control.release.wait()
            except asyncio.CancelledError:
                # Declared late-provider return fault: preserve the genuine
                # original reply, never fabricate one or change auth/DB state.
                control.cancelled_while_gated=True
                await control.release.wait()
            assert loop_time()<deadline,'gate must not extend original physical deadline'
            control.real_reply_delivered=True
        return answer
    monkeypatch.setattr(inner,'generate',physical_boundary)
    created=create_session(client,headers,key());assert created.status_code==201,created.text
    sid=created.json()['session_id'];runtime=physical.manager.runtimes[sid];original_audio=runtime.audio
    events=dictation(client,headers,sid);assert events[-1]['type']=='result.ready',events[-1]
    result=events[-1]['result'];assert result['raw_text']==result['refined_text']==RAW
    assert result['review_required']is True and result['review_reason']=='llm_unavailable'
    assert control.calls==[(1,'refine')]
    async def settled():await wait_until(lambda:runtime.task is None or runtime.task.done())
    client.portal.call(settled)
    assert original_audio is runtime.audio and len(original_audio)==0 and runtime.asr is None and runtime.retry_expires_at is None
    initial=business_snapshot(owner)
    assert len(initial['sessions'])==len(initial['session_attempts'])==len(initial['results'])==1
    assert initial['sessions'][0]['state']=='ready'and initial['results'][0]['review_required']is True
    assert [(x['purpose'],x['status'],x['error_code'])for x in initial['model_calls']]==[('refine','failed','unavailable')]
    assert initial['insertions']==initial['learning_events']==initial['dictionary_terms']==[]
    assert all(x['version']==0 for x in initial['dictionary_state'])
    return sid,result,physical,control,initial


def origin(row):return {k:row[k]for k in ORIGIN_FIELDS}

async def terminal_detail(http,headers,sid):
    # Original runtime-task completion can precede its background terminal CAS;
    # observe the formal committed state rather than race the correct cleanup.
    async with asyncio.timeout(8):
        while True:
            response=await http.get(f'/api/v1/sessions/{sid}',headers=headers)
            assert response.status_code==200,response.text
            body=response.json()
            if body['current_attempt_no']==2 and body['state']in('ready','failed','cancelled','empty'):return response
            await asyncio.sleep(0.01)

def retry(client,headers,sid,idem,kind='refine'):
    return client.post(f'/api/v1/sessions/{sid}/retry',headers={**headers,'Idempotency-Key':idem},json={'retry_kind':kind})


def assert_ready_retry(client,owner,sid,result,initial,physical,control):
    current=business_snapshot(owner);session=current['sessions'][0]
    assert len(current['sessions'])==1 and session['id']==sid and session['state']=='ready'and session['current_attempt_no']==2
    assert origin(session)==origin(initial['sessions'][0])
    old=next(x for x in current['results']if x['id']==result['result_id']);assert old==initial['results'][0]
    new=next(x for x in current['results']if x['id']==session['current_result_id'])
    assert new['id']!=old['id']and new['attempt_no']==2 and new['raw_text']==RAW and new['refined_text']==REFINED and new['review_required']is False
    assert sorted((x['attempt_no'],x['kind'],x['state'])for x in current['session_attempts'])==[(1,'initial','ready'),(2,'refine','ready')]
    assert len(physical.opened)==1,'refine retry must not open ASR'
    assert control.calls==[(1,'refine'),(2,'refine')]
    fresh=[x for x in current['model_calls']if x['attempt_no']==2]
    assert sorted(x['purpose']for x in fresh)==['refine']
    assert all(x['status']=='succeeded'and x['ended_at']is not None and x['aggregated_at']is not None for x in fresh)
    assert current['insertions']==current['learning_events']==current['dictionary_terms']==[]
    assert all(x['version']==0 for x in current['dictionary_state'])
    contribution=current['session_contributions'];assert len(contribution)==1
    assert contribution[0]['inserted']is False and contribution[0]['received_samples']==8000 and contribution[0]['successful_audio_samples']==8000
    assert contribution[0]['char_count']==sum(ch.isalnum()for ch in REFINED)
    assert sum(x['sessions']for x in current['usage_daily'])==sum(x['result_sessions']for x in current['usage_daily'])==1
    assert sum(x['inserted_sessions']for x in current['usage_daily'])==sum(x['learning_events']for x in current['usage_daily'])==0
    return current,new


def test_other_desktop_refine_and_cross_device_same_key_replay(atomic_client,monkeypatch,record_property):
    client=atomic_client;owner,headers,devices,sids=pair(client)
    sid,result,physical,control,initial=prepare_fallback(client,monkeypatch,owner,headers[0])
    for h in headers:
        detail=client.get(f'/api/v1/sessions/{sid}',headers=h);assert detail.status_code==200,detail.text
        assert detail.json()['retry_kind']=='refine'
        history=client.get('/api/v1/history',headers=h);assert history.status_code==200,history.text
        item=next(x for x in history.json()['items']if x['session_id']==sid);assert item['retry_kind']=='refine'
    idem=key();response=retry(client,headers[1],sid,idem)
    assert response.status_code==202,response.text
    assert response.json()['current_attempt_no']==2 and response.json()['replayed']is False
    final=wait_state(client,headers[1],sid);assert final['state']=='ready',final
    current,new=assert_ready_retry(client,owner,sid,result,initial,physical,control)
    assert origin(current['sessions'][0])['device_id']==devices[0]and origin(current['sessions'][0])['created_login_sid']==sids[0]
    for h in headers:
        before=business_snapshot(owner);counts=(len(physical.opened),list(control.calls))
        replay=retry(client,h,sid,idem);assert replay.status_code==200,replay.text
        assert replay.json()['replayed']is True and replay.json()['result']['result_id']==new['id']and replay.json()['current_attempt_no']==2
        assert business_snapshot(owner)==before and (len(physical.opened),control.calls)==counts
    record_property('hist10_other_desktop',json.dumps({'session_id':sid,'origin_device_id':devices[0],'origin_login_sid':sids[0],'requesting_login_sid':sids[1],'raw_sha256':hashlib.sha256(RAW.encode()).hexdigest(),'refine_attempt':2,'asr_opens':len(physical.opened),'physical_llm_calls':control.calls,'no_insertion_or_learning':True}))


def test_asr_retry_and_recording_ws_remain_creator_bound(atomic_client,monkeypatch):
    client=atomic_client;owner,headers,_,_=pair(client)
    physical=AudioFixture(client,monkeypatch,{owner:RAW});retained=_failed_audio(client,headers[0],physical)
    before=business_snapshot(owner);cutoff=retained.runtime.retry_expires_at;pcm_hash=hashlib.sha256(retained.audio).hexdigest();opens=list(physical.opened)
    for _ in range(2):
        denied=retry(client,headers[1],retained.sid,key(),'asr');assert denied.status_code==403,denied.text
        assert denied.json()['error']['code']=='FORBIDDEN'and business_snapshot(owner)==before
        assert retained.runtime.audio is retained.audio and hashlib.sha256(retained.audio).hexdigest()==pcm_hash and retained.runtime.retry_expires_at==cutoff
        assert physical.opened==opens
    idem=key();accepted=retry(client,headers[0],retained.sid,idem,'asr');assert accepted.status_code==202,accepted.text
    done=wait_state(client,headers[0],retained.sid);assert done['state']=='ready',done
    finished=business_snapshot(owner);denied=retry(client,headers[1],retained.sid,idem,'asr')
    assert denied.status_code==403 and denied.json()['error']['code']=='FORBIDDEN'
    assert business_snapshot(owner)==finished and len(retained.audio)==0
    created=create_session(client,headers[0],key());assert created.status_code==201,created.text
    pending=created.json()['session_id'];runtime=physical.manager.runtimes[pending];before=business_snapshot(owner)
    # Exact existing contract: HTTP403 before upgrade, never invented WS4403.
    assert_handshake_denied(client,f'/ws/v1/sessions/{pending}/audio',headers[1],403,'FORBIDDEN')
    assert runtime.connected is False and business_snapshot(owner)==before
    with client.websocket_connect(f'/ws/v1/sessions/{pending}/audio',headers=headers[0])as ws:
        assert ws.receive_json()['type']=='ready';cancel_and_drain(ws)


@pytest.mark.parametrize('logout_actor',['current_caller','origin_only'])
def test_refine_uses_current_caller_sid_not_session_origin(atomic_client,monkeypatch,record_property,logout_actor):
    client=atomic_client;owner,headers,devices,sids=pair(client)
    sid,result,physical,control,initial=prepare_fallback(client,monkeypatch,owner,headers[0])
    async def exercise():
        control.entered=asyncio.Event();control.release=asyncio.Event();control.gate_retry=True
        manager=client.app.state.sessions;runtime=None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),base_url='http://hist10-current-caller')as http:
            try:
                response=await http.post(f'/api/v1/sessions/{sid}/retry',headers={**headers[1],'Idempotency-Key':key()},json={'retry_kind':'refine'})
                assert response.status_code==202,response.text
                await wait_until(control.entered.is_set)
                runtime=manager.runtimes[sid]
                assert runtime.attempt_no==2 and runtime.ctx.sid==sids[1]and runtime.device_id==devices[0]
                async with client.app.state.db.session_factory()as db:
                    row=(await db.execute(text('SELECT device_id,created_login_sid,current_attempt_no FROM sessions WHERE user_id=:u AND id=:s'),{'u':owner,'s':sid})).one()
                    assert tuple(row)==(devices[0],sids[0],2)
                index=1 if logout_actor=='current_caller'else 0
                logged_out=await http.post('/api/auth/logout',headers=headers[index]);assert logged_out.status_code==204,logged_out.text
                async with client.app.state.db.session_factory()as db:
                    revoked=dict((await db.execute(text('SELECT id,revoked_at FROM login_sessions WHERE user_id=:u'),{'u':owner})).all())
                    assert revoked[sids[index]]is not None and revoked[sids[1-index]]is None
                # Release the original valid provider response after actual
                # caller/origin revocation committed; never edit session state.
                control.release.set();await wait_until(lambda:runtime.task is not None and runtime.task.done())
                assert control.real_reply_delivered and control.original_reply.error is None
                active=headers[0]if logout_actor=='current_caller'else headers[1]
                detail=await terminal_detail(http,active,sid)
                if logout_actor=='current_caller':
                    assert detail.json()['state']=='failed'and detail.json()['error_code']=='AUTH_INVALID',detail.json()
                    assert runtime.cancelled or runtime.error_code=='AUTH_INVALID'
                else:
                    assert detail.json()['state']=='ready'and detail.json()['result']['attempt_no']==2 and detail.json()['result']['refined_text']==REFINED,detail.json()
                return {'current_caller_sid':runtime.ctx.sid,'origin_sid':sids[0],'logout_actor':logout_actor,'late_original_reply_delivered':control.real_reply_delivered,'cancelled_while_gated':control.cancelled_while_gated,'terminal_state':detail.json()['state']}
            finally:
                control.release.set()
                if runtime is not None and runtime.task is not None and not runtime.task.done():await wait_until(runtime.task.done)
    evidence=client.portal.call(exercise)
    final=business_snapshot(owner);assert origin(final['sessions'][0])==origin(initial['sessions'][0])
    assert len(physical.opened)==1 and final['insertions']==final['learning_events']==final['dictionary_terms']==[]
    old=next(x for x in final['results']if x['id']==result['result_id']);assert old==initial['results'][0]
    if logout_actor=='current_caller':
        assert len(final['results'])==1 and final['sessions'][0]['current_result_id']is None
        attempt=next(x for x in final['session_attempts']if x['attempt_no']==2);assert attempt['state']=='failed'and attempt['error_code']=='AUTH_INVALID'
    else:assert_ready_retry(client,owner,sid,result,initial,physical,control)
    record_property('hist10_caller_sid',json.dumps(evidence))


def test_other_tenant_and_admin_web_cannot_retry_owned_history(atomic_client,monkeypatch):
    client=atomic_client;owner,headers,_,_=pair(client)
    sid,_,physical,control,_=prepare_fallback(client,monkeypatch,owner,headers[0])
    outsider=register_and_login(client,email='hist10-outsider-'+key()+'@example.invalid');other=outsider['user']['id'];h=auth_headers(outsider)
    admin=login_admin(client);admin_id=admin['user']['id'];admin_h=auth_headers(admin)
    before=business_snapshot(owner,other,admin_id);counts=(len(physical.opened),list(control.calls))
    missing=client.get(f'/api/v1/sessions/{sid}',headers=h);assert missing.status_code==404 and missing.json()['error']['code']=='NOT_FOUND'
    forbidden=retry(client,h,sid,key());assert forbidden.status_code==404 and forbidden.json()['error']['code']=='NOT_FOUND'
    forbidden=retry(client,admin_h,sid,key());assert forbidden.status_code==403 and forbidden.json()['error']['code']=='FORBIDDEN'
    history=client.get('/api/v1/history',headers=h);assert history.status_code==200 and history.json()['items']==[]
    assert business_snapshot(owner,other,admin_id)==before and (len(physical.opened),control.calls)==counts
