"""Original R2/Qwen budgets, real local HTTP/ASGI/PG; fictional inference only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import asyncio
import json
import time
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select
import yaml

from app.core.config import Config
from app.models import ModelCall, ModelUsageDaily
from app.providers.asr.qwen3_mlx import Qwen3MlxProvider
from app.services.r2_prompt import R2_SYSTEM
from tests.conftest import auth_headers, register_and_login, wait_for_readiness
from tests.test_sessions import create_session, dictation
from tests.test_insertions_learning import claim, receipt
from tests.integration.test_normal_shutdown_contract import WsProbe
from tests.unit.test_qwen_native_provider import Process, manifest

RAW='请用 SQL lite 保存日志，不要修改配置。'
BODY='请用 SQL lite 保存日志，不要修改配置。'


async def facts(app,sid):
    async with app.state.db.session_factory() as db:
        calls=(await db.scalars(select(ModelCall).where(ModelCall.session_id==sid)
            .order_by(ModelCall.started_at,ModelCall.id))).all()
        return [{k:getattr(r,k) for k in ('purpose','provider','model_id','status','error_code',
            'prompt_tokens','completion_tokens','ended_at','aggregated_at','elapsed_ms')} for r in calls]


def assert_budgets(c):
    assert (c.asr.finish_timeout_ms,c.llm.call_timeout_ms,c.pipeline.stop_to_result_budget_ms,
        c.pipeline.classification_budget_ms,c.llm.foreground_wait_ms,c.llm.classify_wait_ms,
        c.llm.automatic_retries_per_attempt)==(10000,10000,30000,10000,2000,1000,1)


def trace_calls(client,monkeypatch,*,classification=False):
    root=client.app.state.llm
    pool=root.classify if classification else getattr(root,'refine',root)
    outer,physical=[],[]
    actual,inner=pool.generate,pool.inner.generate
    async def observed(request,deadline):
        outer.append({'purpose':request.purpose,'total_deadline':deadline,'started':asyncio.get_running_loop().time()})
        return await actual(request,deadline)
    async def dispatched(request,deadline):
        physical.append({'purpose':request.purpose,'call_id':request.call_id,'deadline':deadline,
            'started':asyncio.get_running_loop().time(),'output_mode':request.output_mode})
        return await inner(request,deadline)
    monkeypatch.setattr(pool,'generate',observed);monkeypatch.setattr(pool.inner,'generate',dispatched)
    return pool,outer,physical


def text_sse(value):
    def event(delta,finish=None):
        return ('data: '+json.dumps({'choices':[{'index':0,'delta':delta,'finish_reason':finish}]},ensure_ascii=False)+'\n\n').encode()
    return event({'content':value})+event({},'stop')+b'data: [DONE]\n\n'


@contextmanager
def upstream(mode):
    records=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            self.reply(b'{"data":[{"id":"fixture"}]}','application/json')
        def reply(self,data,kind):
            self.send_response(200);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(data)));self.end_headers()
            try:self.wfile.write(data);self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if request['messages'][0]['content'].startswith('svi-r2-text-health-v1'):
                self.reply(text_sse('ready'),'text/event-stream');return
            row={'received_mono':time.monotonic(),'request':request};records.append(row);nth=len(records)
            first=nth==1
            status=(429 if (mode in {'r2-cooldown','classify-rate429'} and first) else
                401 if mode=='classify-auth401' else
                503 if mode in {'r2-retry-timeout','classify-503-recover'} and first else None)
            if status:
                row['status']=status;row['headers_send_mono']=time.monotonic()
                self.send_response(status);self.send_header('Retry-After','5')
                self.send_header('Content-Length','0');self.end_headers();return
            if mode=='r2-retry-timeout' or mode=='classify-retry-timeout' and nth==2:
                if request.get('stream'):
                    self.send_response(200);self.send_header('Content-Type','text/event-stream')
                    self.send_header('Connection','close');self.end_headers()
                    try:
                        until=time.monotonic()+12
                        while time.monotonic()<until:
                            self.wfile.write(b': fictional heartbeat\n\n');self.wfile.flush();time.sleep(.1)
                    except (BrokenPipeError,ConnectionResetError):pass
                    self.close_connection=True;return
                # Blocking only the local mock upstream; real client timeout remains unchanged.
                time.sleep(11)
            if request.get('stream'):
                self.reply(text_sse(BODY),'text/event-stream');return
            original=json.loads(request['messages'][1]['content'])
            parsed={'decision':'term','ambiguous':False,'category':'spelling','reason_code':'FICTIONAL_PROTOCOL',
                'evidence':original['expected_evidence']}
            if mode in {'classify-schema-recover','classify-retry-timeout'} and first:
                if mode=='classify-retry-timeout':time.sleep(.35)
                parsed={'wrong':'schema'}
            if mode=='classify-evidence':parsed['evidence']={**parsed['evidence'],'before':'wrong'}
            if mode=='classify-combination':parsed.update(decision='suggest',ambiguous=False)
            response={'choices':[{'message':{'content':json.dumps(parsed,ensure_ascii=False)},
                'finish_reason':'length' if mode=='classify-truncated' else 'stop'}]}
            self.reply(json.dumps(response).encode(),'application/json')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}',records
    finally:server.shutdown();server.server_close();thread.join(2)


@contextmanager
def http_app(test_database,tmp_path,monkeypatch,mode,*,classification=False):
    from app.main import create_app
    monkeypatch.setenv('SVI_INSTANCE_LOCK','0')
    config=yaml.safe_load(Path('config/app.yaml').read_text())
    config['asr']['provider']='mock'
    with upstream(mode) as (base,records):
        setting=dict(config['llm']);setting.update(provider='openai_compatible',base_url=base,
            model_id='fictional-classification' if classification else 'deepseek-flash',thinking='disabled',
            refine_protocol='ds_r2',api_key_env=None,max_output_tokens=8192)
        if classification:
            config['llm']['provider']='mock';config['classification_llm']=setting
        else:config['llm']=setting;config['classification_llm']=None
        path=tmp_path/'budget.yaml';path.write_text(yaml.safe_dump(config))
        with TestClient(create_app(path)) as client:
            wait_for_readiness(client);assert_budgets(client.app.state.config)
            if classification:
                c=client.app.state.config.classification_llm
                assert (c.call_timeout_ms,c.classify_wait_ms,c.automatic_retries_per_attempt)==(10000,1000,1)
            client.app.state.asr.text=RAW
            yield client,records


def completed(client,h,label):
    created=create_session(client,h,label);assert created.status_code==201,created.text
    sid=created.json()['session_id'];runtime=client.app.state.sessions.runtimes[sid]
    terminal=dictation(client,h,sid)[-1];assert terminal['type']=='result.ready'
    assert runtime.audio==b'' and runtime.asr is None
    return sid,terminal['result']


def test_r2_real_retry_after_cooldown_blocks_early_dispatch_then_recovers(
    test_database,tmp_path,monkeypatch,record_property,
):
    with http_app(test_database,tmp_path,monkeypatch,'r2-cooldown') as (client,wire):
        actor=register_and_login(client,email='r2-cooldown-new@example.invalid');h=auth_headers(actor)
        pool,outer,physical=trace_calls(client,monkeypatch)
        first,result=completed(client,h,'cooldown-first');assert result['review_required']
        assert result['review_reason']=='llm_unavailable' and len(wire)==1
        started=wire[0]['headers_send_mono'];until=started+5
        second,result=completed(client,h,'cooldown-blocked');assert result['review_required']
        assert len(wire)==1 and client.portal.call(facts,client.app,second)==[]
        time.sleep(max(0,until-time.monotonic())+.01)
        third,result=completed(client,h,'cooldown-recovered')
        assert not result['review_required'] and result['refined_text']==BODY
        assert len(wire)==len(physical)==2 and wire[1]['received_mono']>=until
        for item in wire:
            assert item['request']['messages'][0]['content']==R2_SYSTEM
            assert json.loads(item['request']['messages'][1]['content'])=={'transcript':RAW,'confirmed_terms':[]}
        assert [r['status'] for r in client.portal.call(facts,client.app,first)]==['failed']
        assert [r['status'] for r in client.portal.call(facts,client.app,third)]==['succeeded']
        assert pool.scheduler.snapshot()=={'running':0,'high_waiting':0,'low_waiting':0}
        record_property('r2_cooldown',json.dumps({'retry_after_seconds':5,'next_request_after_ms':
            (wire[1]['received_mono']-started)*1000,'actual_requests':2,'blocked_model_rows':0,'trace':outer}))


def test_r2_503_retry_then_heartbeat_shares_original_total_and_never_calls_a_third_time(
    test_database,tmp_path,monkeypatch,record_property,
):
    with http_app(test_database,tmp_path,monkeypatch,'r2-retry-timeout') as (client,wire):
        actor=register_and_login(client,email='r2-retry-budget@example.invalid');h=auth_headers(actor)
        _,outer,physical=trace_calls(client,monkeypatch)
        sid,result=completed(client,h,'retry-budget')
        assert result['review_required'] and result['review_reason']=='llm_timeout'
        assert result['raw_text']==result['refined_text']==RAW and len(wire)==len(outer)==len(physical)==2
        assert outer[0]['total_deadline']==outer[1]['total_deadline']
        assert physical[1]['deadline']<=outer[0]['total_deadline']
        assert 9.5<physical[1]['deadline']-physical[1]['started']<=10
        assert all(0<x['deadline']-x['started']<=10 for x in physical)
        assert wire[0]['request']==wire[1]['request']
        calls=client.portal.call(facts,client.app,sid)
        assert [r['status'] for r in calls]==['failed','unknown']
        assert all(r['ended_at'] is not None and r['aggregated_at'] is not None for r in calls)
        assert all(r['prompt_tokens'] is r['completion_tokens'] is None for r in calls)
        assert 9500<=result['stop_to_result_ms']<=30000
        assert claim(client,h,sid,result).status_code==409
        record_property('r2_retry_original_budget',json.dumps({'calls':2,'outer':outer,'physical':physical,
            'stop_to_result_ms':result['stop_to_result_ms'],'review':True,'native':False}))


@pytest.mark.parametrize('mode,expected,calls,reason',[
    ('503-recover','learned',2,None),('schema-recover','learned',2,None),
    ('auth401','failed',1,'MODEL_BUSY'),('rate429','failed',1,'MODEL_BUSY'),
    ('truncated','failed',1,'MODEL_BUSY'),('evidence','failed',1,'EVIDENCE_MISMATCH'),
    ('combination','failed',1,'INVALID_CLASSIFICATION'),('retry-timeout','failed',2,'MODEL_BUSY'),
])
def test_independent_classification_real_http_retry_guard_and_original_total(
    test_database,tmp_path,monkeypatch,record_property,mode,expected,calls,reason,
):
    with http_app(test_database,tmp_path,monkeypatch,'classify-'+mode,classification=True) as (client,wire):
        actor=register_and_login(client,email='classify-budget-'+mode+'@example.invalid');h=auth_headers(actor)
        _,outer,physical=trace_calls(client,monkeypatch,classification=True)
        sid,result=completed(client,h,'classification-source-'+mode)
        iid=claim(client,h,sid,result).json()['insertion_id']
        assert receipt(client,h,sid,iid,'inserted',True).status_code==200  # Explicit synthetic API precondition, no OS write.
        start=time.monotonic();offset=BODY.index('SQL lite')
        made=client.post('/api/v1/corrections',headers=h,json={'session_id':sid,'insertion_id':iid,
            'correction_id':str(uuid.uuid4()),'offset':offset,'before':'SQL lite','after':'SQLite',
            'left':BODY[max(0,offset-4):offset],'right':BODY[offset+8:offset+12]})
        assert made.status_code==202,made.text;event_id=made.json()['event_id']
        limit=time.monotonic()+12
        while True:
            event=client.get('/api/v1/learning-events/'+event_id,headers=h).json()
            if event['status']!='pending':break
            assert time.monotonic()<limit;time.sleep(.2)
        elapsed=time.monotonic()-start
        assert (event['status'],event['reason'])==(expected,reason),event
        assert len(wire)==len(outer)==len(physical)==calls
        assert len({x['total_deadline'] for x in outer})==1
        assert all(x['purpose']=='classify' and x['output_mode']=='json' for x in physical)
        assert all(x['deadline']<=outer[0]['total_deadline'] and 0<x['deadline']-x['started']<=10 for x in physical)
        assert all('stream' not in x['request'] and x['request']['max_tokens']==256 for x in wire)
        if calls==2:assert wire[0]['request']==wire[1]['request']
        rows=client.portal.call(facts,client.app,sid);class_rows=[x for x in rows if x['purpose']=='classify']
        assert len(class_rows)==calls and all(x['provider']=='openai_compatible' for x in class_rows)
        assert all(x['prompt_tokens'] is x['completion_tokens'] is None for x in class_rows)
        assert all(x['ended_at'] is not None and x['aggregated_at'] is not None for x in class_rows)
        terms=client.get('/api/v1/dictionary/terms',headers=h).json()['items']
        assert ([x['term'] for x in terms]==['SQLite']) if expected=='learned' else terms==[]
        if mode=='retry-timeout':
            assert physical[1]['deadline']-physical[1]['started']<9.0
            assert 8.5<elapsed<10.5  # HTTP observation lag separated from original model deadline.
            assert class_rows[-1]['status']=='unknown'
        record_property('independent_classification_budget',json.dumps({'mode':mode,'requests':calls,
            'event_status':event['status'],'reason':event['reason'],'outer':outer,'physical':physical,
            'observed_event_seconds':elapsed,'synthetic_receipt_only':True,'public_calls':0}))


class DeadlineWorker:
    stop_incomplete=False
    def __init__(self):
        self.process=None;self.calls=[];self.states={};self.active=None
        self.first=None;self.timeout_owner=None;self.entered=asyncio.Event();self.release=asyncio.Event()
    async def decode(self,args,deadline):
        audio,cmd=args;action=cmd['action'];owner=cmd.get('owner')
        if action=='prepare':self.process=Process();return {'ready':True,'runtime':'fictional-deadline'}
        if action=='drop':self.states.pop(owner,None);return {'dropped':True}
        assert action=='finish' and len(audio)==8000 and cmd['start_sample']==0
        trace={'owner':owner,'deadline':deadline,'started':asyncio.get_running_loop().time()};self.calls.append(trace)
        self.active=audio
        try:
            async with asyncio.timeout_at(deadline):
                if owner==self.first:self.entered.set();await self.release.wait()
                if owner==self.timeout_owner:await asyncio.Future()
                return {'text':RAW,'stable':'','samples':4000}
        finally:self.active=None;trace['returned']=asyncio.get_running_loop().time()
    async def stop(self):self.states.clear();self.process=None


@pytest.fixture
def qwen_budget(clean_database,monkeypatch,tmp_path,request):
    from app import main
    folder,path=manifest(tmp_path);worker=DeadlineWorker();load=main.load_config
    def configured(path=None):
        base=load(path);data=base.model_dump();data['asr'].update(provider='qwen3_mlx',model_id='fixture-qwen-budget',
            model_path=str(folder),model_manifest_path=str(tmp_path/'manifest.json'),execution_slots=1)
        checked=Config.model_validate(data);return base.model_copy(update={'asr':checked.asr})
    def build(config):return Qwen3MlxProvider(config.asr,queue_limit=config.effective_admission_ceiling,worker=worker)
    monkeypatch.setattr(main,'load_config',configured);monkeypatch.setattr(main,'build_asr',build)
    return request.getfixturevalue('app_client'),worker


def test_qwen_finish_original_ten_seconds_includes_wait_and_terminal_bookkeeping(qwen_budget,record_property):
    client,worker=qwen_budget;app=client.app;assert_budgets(app.state.config)
    actors=[register_and_login(client,email='qwen-budget-'+str(n)+'@example.invalid') for n in range(2)]
    async def run():
        loop=asyncio.get_running_loop();wires=[];jobs=[];originals=[]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as http:
            async def begin(actor):
                h=auth_headers(actor);label=str(uuid.uuid4())
                r=await http.post('/api/v1/sessions',headers={**h,'Idempotency-Key':label},
                    json={'client_request_id':label,'mode':'dictation','learning_enabled':False})
                assert r.status_code==201;r=r.json();sid=r['session_id']
                wire=WsProbe(app,h,sid);wires.append(wire);await wire.start()
                runtime=app.state.sessions.runtimes[sid];originals.append(runtime.audio)
                return sid,h,wire,runtime
            first=await begin(actors[0]);worker.first=(actors[0]['user']['id'],first[0],1)
            try:
                await first[2].end();jobs.append(asyncio.create_task(first[2].drain()))
                async with asyncio.timeout(2):await worker.entered.wait()
                second=await begin(actors[1]);owner=(actors[1]['user']['id'],second[0],1);worker.timeout_owner=owner
                await second[2].end();jobs.append(asyncio.create_task(second[2].drain()))
                end=second[3].end_accepted_at;deadline=end+10
                assert second[3].deadline==end+30
                scheduler=app.state.asr.scheduler
                async with asyncio.timeout(2):
                    while owner not in scheduler.queues['high']:await asyncio.sleep(.001)
                job=scheduler.queues['high'][owner];assert job.deadline<=deadline
                await asyncio.sleep(2);worker.release.set()
                async with asyncio.timeout(12):await asyncio.gather(*jobs)
                good=(await http.get('/api/v1/sessions/'+first[0],headers=first[1])).json()
                bad=(await http.get('/api/v1/sessions/'+second[0],headers=second[1])).json()
                assert good['state']=='ready' and good['result']['raw_text']==RAW
                assert bad['state']=='failed' and bad['error_code']=='ASR_TIMEOUT' and bad['result'] is None
                assert len(worker.calls)==2 and worker.calls[1]['owner']==owner
                trace=worker.calls[1];assert trace['started']-end>=1.9
                assert trace['deadline']<=deadline and trace['deadline']-trace['started']<8
                assert loop.time()<=deadline and worker.active is None and not app.state.asr.engine._audio_inputs
                rows=await facts(app,second[0]);assert len(rows)==1
                assert rows[0]['purpose']=='asr_finish' and rows[0]['status']!='running'
                assert rows[0]['ended_at'] is not None and rows[0]['aggregated_at'] is not None
                assert scheduler.snapshot()=={'running':0,'high_waiting':0,'low_waiting':0}
                assert originals[0]==b'' and len(originals[1])==8000  # Complete failure cache is intentionally retained.
                record_property('qwen_original_finish_budget',json.dumps({'queued_seconds':trace['started']-end,
                    'physical_remaining_seconds':trace['deadline']-trace['started'],
                    'total_observed_seconds':loop.time()-end,'finish_deadline':deadline,'worker_calls':worker.calls,
                    'cache_bytes':len(originals[1]),'working_copy_cleared':True,'physical_worker':'fictional'}))
            finally:
                worker.release.set()
                for task in jobs:
                    if not task.done():task.cancel()
                await asyncio.gather(*jobs,return_exceptions=True)
                for wire in wires:
                    if wire.task and not wire.task.done():wire.task.cancel()
                await asyncio.gather(*(w.task for w in wires if w.task),return_exceptions=True)
    client.portal.call(run)
