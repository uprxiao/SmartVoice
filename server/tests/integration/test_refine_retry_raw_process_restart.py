"""Real PG/API/WS + owned process crash; initial/refine retry restart comparison.

Only physical Mock provider gates are replaced. No durable state/raw/result is
injected. This does not use the live GUI DB/service, models or microphone.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import multiprocessing
import os
import re
import signal
import traceback
import uuid

import pytest
from sqlalchemy.engine import make_url
from tests.conftest import SERVER_ROOT, TEST_DB, TEST_URL, wait_for_readiness_async
from tests.integration.test_voice_process_restart import (
    PASSWORD, advisory_owners, bundle, complete_session, connect_control,
    fingerprint, login, new_session, receive,
)

RAW = '嗯那个，保留接口文档，不要删除日志。'
REFINED = '保留接口文档，不要删除日志。'


async def child_run(pipe, parent, nonce, kind, role, prior):
    assert os.getppid() == parent and multiprocessing.parent_process().pid == parent
    assert re.fullmatch(r'smartvoiceinput_test_[a-zA-Z0-9_]{1,38}', TEST_DB)
    assert make_url(TEST_URL).database == os.environ['SVI_TEST_DB_NAME'] == TEST_DB
    assert os.environ['SVI_DATABASE_URL'] == TEST_URL
    import httpx
    from app import main
    from app.providers.asr.base import ProviderError
    from app.providers.asr.mock import MockAsrProvider
    from app.providers.llm.base import LlmResponse
    from app.providers.llm.mock import MockLlmProvider
    from app.services.inference_context import current_scope
    from tests.integration.test_normal_shutdown_contract import WsProbe

    assert type(asyncio.get_running_loop()).__module__ == 'uvloop'
    pipe.send({'event':'owned_child', 'pid':os.getpid(), 'ppid':os.getppid(), 'nonce':nonce,
               'database':TEST_DB, 'role':role, 'loop_class':'uvloop.Loop'})
    target = {}; entered = asyncio.Event(); release = asyncio.Event(); gates = {}
    calls, asr_calls = [], []
    original_open, original_generate = MockAsrProvider.open, MockLlmProvider.generate

    async def asr_open(provider, options):
        asr_calls.append((options.session_id, options.attempt_no))
        stream = await original_open(provider, options)
        stream.text = RAW  # Explicit synthetic Mock transcript, not an ASR quality assertion.
        return stream

    async def generate(provider, request, deadline):
        scope = current_scope.get()
        assert scope is not None
        calls.append((scope.session_id, scope.attempt_no, request.purpose))
        if role == 'A' and scope.session_id == target.get('sid') and request.purpose == 'refine':
            assert request.call_id and request.input['transcript'] == RAW
            if kind == 'refine' and scope.attempt_no == 1:
                # All permitted first-attempt provider retries fail, never just one dispatch.
                return LlmResponse(parsed=None, provider=provider.provider_id, model_id=provider.model_id,
                    finish_reason='error', error=ProviderError('unavailable','synthetic first-attempt failure',retryable=True))
            assert scope.attempt_no == (2 if kind == 'refine' else 1)
            gates['deadline'] = deadline; entered.set()
            await release.wait()  # Original provider budget; parent kills exact owned A while held.
            raise AssertionError('parent unexpectedly released crash gate')
        return await original_generate(provider, request, deadline)

    MockAsrProvider.open, MockLlmProvider.generate = asr_open, generate
    app = main.create_app(str(SERVER_ROOT/'config/app.yaml'))
    control = None; close = {}
    try:
        async with app.router.lifespan_context(app):
            # Consume the existing parent's 20s receive window, without a new
            # model/HTTP deadline or manually calling the readiness probe.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url='http://testserver') as health_client:
                await wait_for_readiness_async(health_client)
            assert app.state.ready and not app.state.instance_fenced
            assert app.state.config.asr.provider == app.state.config.llm.provider == 'mock'
            assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
            assert app.state.config.llm.call_timeout_ms == app.state.config.asr.finish_timeout_ms == 10000
            actual_close = app.state.db.close
            async def observe_close():
                close['checkedout_before_dispose'] = app.state.db.engine.pool.checkedout()
                await actual_close(); close['completed'] = True
            app.state.db.close = observe_close
            control = await connect_control()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as http:
                if role == 'A':
                    registration = await http.post('/api/auth/register',json={
                        'email':f'voice12-{nonce}@example.invalid','password':PASSWORD})
                    assert registration.status_code == 201
                headers, user = await login(http, nonce)
                if role == 'A':
                    history_sid = await complete_session(app,http,headers)
                    history_hash = fingerprint(await bundle(control,user,history_sid))
                    sid = await new_session(http,headers); target['sid'] = sid
                    probe = await WsProbe(app,headers,sid).start(); await probe.end()
                    if kind == 'refine':
                        await probe.drain(); assert probe.close_code == 1000
                        first = await bundle(control,user,sid)
                        assert first['sessions'][0]['state'] == first['session_attempts'][0]['state'] == 'ready'
                        assert len(first['results']) == 1
                        assert first['results'][0]['raw_text'] == first['results'][0]['refined_text'] == RAW
                        assert first['results'][0]['review_reason'] == 'llm_unavailable'
                        assert sid not in app.state.sessions.runtimes
                        retried = await http.post(f'/api/v1/sessions/{sid}/retry',headers=headers|{'Idempotency-Key':str(uuid.uuid4())},json={'retry_kind':'refine'})
                        assert retried.status_code == 202
                    else:
                        first = None
                        probe.drain_task = asyncio.create_task(probe.drain())
                    async with asyncio.timeout(3): await entered.wait()
                    before = await bundle(control,user,sid)
                    session = before['sessions'][0]
                    attempts = sorted(before['session_attempts'],key=lambda row:row['attempt_no'])
                    current = attempts[-1]; expected_no = 2 if kind == 'refine' else 1
                    assert session['state'] == 'refining' and session['current_attempt_no'] == expected_no
                    assert session['current_result_id'] is None and session['audio_complete'] is True
                    assert session['received_samples'] == 4000 and current['state'] == 'running'
                    assert current['kind'] == kind and current['ended_at'] is None
                    runtime = app.state.sessions.runtimes[sid]
                    assert runtime.raw_text == RAW and runtime.audio == b'' and runtime.asr is None
                    assert runtime.deadline == runtime.end_accepted_at + 30
                    if first:
                        assert before['results'] == first['results']
                        assert attempts[0] == first['session_attempts'][0]
                        assert current['source_attempt_no'] == 1
                    else:
                        assert before['results'] == [] and current['raw_text'] == RAW
                    # Do NOT stop old production here when retry raw is null:
                    # let the real crash/new lifespan prove the resulting failed recovery.
                    lock_key = app.state.config.server.instance_lock_key
                    lock_owners = await advisory_owners(control,lock_key); assert len(lock_owners) == 1
                    pipe.send({'event':'end_committed_and_gated','pid':os.getpid(),'owner':user,'sid':sid,
                        'history_sid':history_sid,'history_hash':history_hash,'bundle':before,'first':first,
                        'current_attempt_raw_present':current['raw_text'] is not None,
                        'current_attempt_raw_equals_source':current['raw_text'] == RAW,
                        'lock_key':lock_key,'lock_backend_pid':lock_owners[0],
                        'remaining_physical_ms':int((gates['deadline']-asyncio.get_running_loop().time())*1000)})
                    await asyncio.Event().wait()
                else:
                    assert prior['owner'] == user
                    sid = prior['sid']; restored = await bundle(control,user,sid)
                    session = restored['sessions'][0]
                    attempts = sorted(restored['session_attempts'],key=lambda row:row['attempt_no'])
                    attempt = attempts[-1]; expected_no=2 if kind == 'refine' else 1
                    assert session['current_attempt_no'] == expected_no
                    # This is the decisive old-production negative oracle.
                    assert session['state'] == attempt['state'] == 'ready', {'actual_state':session['state'],'expected_state':'ready'}
                    assert attempt['raw_text'] == RAW and attempt['error_code'] == 'SERVER_RESTARTED'
                    assert attempt['ended_at'] is not None
                    results = sorted(restored['results'],key=lambda row:row['attempt_no'])
                    assert len(results) == expected_no
                    result=results[-1]
                    assert result['attempt_no'] == expected_no
                    assert result['raw_text'] == result['refined_text'] == RAW
                    assert result['review_required'] is True and result['review_reason'] == 'server_restarted'
                    assert result['text_hash'] == hashlib.sha256(RAW.encode()).hexdigest()
                    assert session['current_result_id'] == result['id'] and session['error_code'] is None
                    assert restored['insertions'] == restored['learning_events'] == []
                    if kind == 'refine':
                        assert results[0] == prior['first']['results'][0]
                        assert attempts[0] == prior['first']['session_attempts'][0]
                        assert attempt['asr_ms'] == 0
                        assert session['first_terminal_at'] == prior['bundle']['sessions'][0]['first_terminal_at']
                    for key in ('id','user_id','device_id','created_login_sid','received_samples','audio_complete','created_at','end_accepted_at'):
                        assert session[key] == prior['bundle']['sessions'][0][key]
                    assert calls == asr_calls == [], 'startup must not dispatch old inference'
                    current_calls=[row for row in restored['model_calls'] if row['attempt_no']==expected_no]
                    assert len(current_calls)==1
                    assert (current_calls[0]['status'],current_calls[0]['error_code'])==('unknown','SERVER_RESTARTED')
                    assert current_calls[0]['prompt_tokens'] is current_calls[0]['completion_tokens'] is None
                    assert current_calls[0]['ended_at'] and current_calls[0]['aggregated_at']
                    detail=await http.get(f'/api/v1/sessions/{sid}',headers=headers)
                    assert detail.status_code==200 and detail.json()['retry_kind']=='refine'
                    assert detail.json()['result']['attempt_kind']==kind
                    assert detail.json()['result']['result_id']==result['id']
                    assert sid not in app.state.sessions.runtimes
                    assert await control.fetchval("SELECT count(*) FROM sessions WHERE state IN ('created','streaming','finalizing','refining')")==0
                    assert fingerprint(await bundle(control,user,prior['history_sid']))==prior['history_hash']
                    stable=fingerprint(restored)
                    # Real recovery is reentrant; the explicit maintenance entry must be a no-op now.
                    await main._recover_interrupted_sessions(app.state.db)
                    assert fingerprint(await bundle(control,user,sid))==stable
                    new_sid=await complete_session(app,http,headers)
                    assert fingerprint(await bundle(control,user,sid))==stable
                    assert [row for row in asr_calls if row[0]==new_sid]==[(new_sid,1)]
                    assert [row[2] for row in calls if row[0]==new_sid]==['refine']
                    evidence={'kind':kind,'recovered_state':session['state'],'attempt_no':expected_no,
                        'raw_restored':True,'review_reason':'server_restarted','old_result_preserved':True,
                        'no_startup_dispatch':True,'new_session_ready':True,'recovery_reentrant':True}
            await control.close(); control=None
        if role=='B':
            assert close=={'checkedout_before_dispose':0,'completed':True}
            pipe.send({'event':'completed','pid':os.getpid(),'evidence':evidence,'close':close})
    finally:
        if control is not None: await control.close()
        MockAsrProvider.open,MockLlmProvider.generate=original_open,original_generate


def child_entry(pipe,parent,nonce,kind,role,prior):
    try:
        import uvloop
        uvloop.run(child_run(pipe,parent,nonce,kind,role,prior))
    except BaseException as error:
        frame=traceback.extract_tb(error.__traceback__)[-1]
        safe={}
        if type(error) is AssertionError and error.args and isinstance(error.args[0],dict):
            safe={key:value for key,value in error.args[0].items() if key in ('actual_state','expected_state') and value in ('ready','failed','refining')}
        pipe.send({'event':'failed','pid':os.getpid(),'exception':type(error).__name__,
                   'reason_detail':safe,'location':{'file':os.path.basename(frame.filename),'line':frame.lineno}})
        raise SystemExit(2) from None
    finally: pipe.close()


def start(context,processes,nonce,kind,role,prior=None):
    reader,writer=context.Pipe(duplex=False)
    process=context.Process(target=child_entry,args=(writer,os.getpid(),nonce,kind,role,prior),
                            name=f'svi_refine_raw_{os.getpid()}_{nonce}_{role}')
    process.start(); writer.close(); processes.append((process,reader))
    hello=receive(reader,process,'owned_child')
    assert hello['ppid']==os.getpid() and hello['nonce']==nonce and hello['database']==TEST_DB
    assert hello['role']==role and hello['loop_class']=='uvloop.Loop'
    return process,reader


@pytest.mark.parametrize('kind',['initial','refine'])
def test_refining_attempt_raw_survives_owned_process_restart(clean_database,monkeypatch,record_property,kind):
    from tests.integration.test_normal_shutdown_contract import require_owned_database
    require_owned_database(); assert os.name=='posix'
    nonce=uuid.uuid4().hex
    for name,value in {'SVI_DATABASE_URL':TEST_URL,'SVI_CONFIG':str(SERVER_ROOT/'config/app.yaml'),
        'SVI_INSTANCE_LOCK':'1','SVI_ENVIRONMENT':'development','SVI_ASR_PROVIDER':'mock','SVI_LLM_PROVIDER':'mock',
        'WEB_CONCURRENCY':'1','UVICORN_WORKERS':'1','SVI_JWT_SECRET':'refine-raw-jwt-'+nonce,
        'SVI_IDEMPOTENCY_SECRET':'refine-raw-idempotency-'+nonce}.items(): monkeypatch.setenv(name,value)
    context=multiprocessing.get_context('spawn'); processes=[]
    evidence={'kind':kind,'scope':'real-owned-process-crash+PG+formal-ASGI+lifespan; Mock inference'}
    try:
        a,reader=start(context,processes,nonce,kind,'A')
        checkpoint=receive(reader,a,'end_committed_and_gated')
        evidence.update(current_attempt_raw_present=checkpoint['current_attempt_raw_present'],
                        current_attempt_raw_equals_source=checkpoint['current_attempt_raw_equals_source'])
        assert checkpoint['remaining_physical_ms']>0 and a.is_alive()
        a.kill(); a.join(5); assert not a.is_alive() and a.exitcode==-signal.SIGKILL
        evidence['a_exitcode']=a.exitcode
        async def unchanged_after_death():
            connection=await connect_control()
            try:
                async with asyncio.timeout(4):
                    while checkpoint['lock_backend_pid'] in await advisory_owners(connection,checkpoint['lock_key']): await asyncio.sleep(.01)
                assert await advisory_owners(connection,checkpoint['lock_key'])==[]
                assert fingerprint(await bundle(connection,checkpoint['owner'],checkpoint['sid']))==fingerprint(checkpoint['bundle'])
            finally: await connection.close()
        asyncio.run(unchanged_after_death())
        b,reader=start(context,processes,nonce,kind,'B',checkpoint)
        assert b.pid!=a.pid
        result=receive(reader,b,'completed'); b.join(5)
        assert not b.is_alive() and b.exitcode==0
        evidence.update(result['evidence'],b_exitcode=b.exitcode,close=result['close'])
    finally:
        for process,pipe in reversed(processes):
            if process.is_alive():
                assert process.name.startswith(f'svi_refine_raw_{os.getpid()}_{nonce}_')
                process.kill(); process.join(5)
            assert not process.is_alive(); pipe.close()
        record_property('refine_retry_raw_restart',json.dumps(evidence,sort_keys=True))
