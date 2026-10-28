"""Real loopback HTTP SSE -> formal app/WS -> owned PG, fictional data only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

from fastapi.testclient import TestClient
import pytest
import yaml

from tests.conftest import auth_headers, register_and_login, wait_for_readiness
from tests.test_sessions import create_session, dictation
from tests.integration.test_refiner_factory_protocol import ledger

RAW = '嗯，先讨论，不改程序。第一项按钮保持蓝色，第二项间距12像素。最后给我计划。'
BODY = '先讨论，不改程序。\n\n1. 按钮保持蓝色。\n2. 间距12像素。\n\n最后给我计划。'


def sse(content, finish='stop', done=True):
    def event(x):return ('data: '+json.dumps(x,ensure_ascii=False)+'\n\n').encode()
    payload = event({'choices':[{'index':0,'delta':{'content':content},'finish_reason':None}]})
    payload += event({'choices':[{'index':0,'delta':{},'finish_reason':finish}]})
    if done:payload += event({'choices':[],'usage':{'prompt_tokens':91,'completion_tokens':37}})+b'data: [DONE]\n\n'
    return payload


@contextmanager
def upstream(mode):
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            data=b'{"data":[{"id":"deepseek-flash"}]}'
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_POST(self):
            value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if value['messages'][0]['content'].startswith('svi-r2-text-health-v1'):
                payload=sse('ready')
            else:
                seen.append(value)
                nth=len(seen)
                if mode in {'auth401','rate429'} or (mode=='http503-recover' and nth==1):
                    self.send_response({'auth401':401,'rate429':429}.get(mode,503))
                    self.send_header('Content-Length','0');self.send_header('Retry-After','5')
                    self.end_headers();return
                if mode=='truncated':payload=sse(BODY[:12],finish='length')
                elif mode=='refused':payload=sse('',finish='content_filter')
                elif mode=='blank-recover' and nth==1:payload=sse(' \n ')
                elif mode=='broken' or (mode=='recover' and nth==1):payload=sse(BODY[:12],done=False)
                else:payload=sse(BODY)
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(payload)));self.end_headers()
            self.wfile.write(payload);self.wfile.flush()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}',seen
    finally:server.shutdown();server.server_close();thread.join(2)


@pytest.mark.parametrize('mode,calls,fallback,reason',[('normal',1,False,None),('recover',2,False,None),
    ('broken',2,True,'llm_invalid_output'),('truncated',1,True,'llm_truncated'),
    ('http503-recover',2,False,None),('blank-recover',2,False,None),
    ('auth401',1,True,'llm_unavailable'),('rate429',1,True,'llm_unavailable'),
    ('refused',1,True,'llm_refused')])
def test_actual_http_sse_formal_ws_result_and_ledger(test_database,tmp_path,monkeypatch,mode,calls,fallback,reason):
    from app.main import create_app
    monkeypatch.setenv('SVI_INSTANCE_LOCK','0')
    config=yaml.safe_load(Path('config/app.yaml').read_text())
    with upstream(mode) as (url,seen):
        config['llm'].update(provider='openai_compatible',base_url=url,model_id='deepseek-flash',
            thinking='disabled',refine_protocol='ds_r2',max_output_tokens=8192)
        path=tmp_path/'r2-http.yaml';path.write_text(yaml.safe_dump(config))
        with TestClient(create_app(path)) as client:
            wait_for_readiness(client)
            pair=register_and_login(client,email=f'r2-{mode}@example.invalid')
            headers=auth_headers(pair)
            client.app.state.asr.text=RAW
            made=create_session(client,headers,f'r2-http-{mode}')
            assert made.status_code==201
            sid=made.json()['session_id']
            events=dictation(client,headers,sid)
            result=events[-1]['result']
            assert result['raw_text']==RAW and result['refined_text']==(RAW if fallback else BODY)
            assert result['review_required'] is fallback and result['review_reason']==reason
            assert len(seen)==calls
            assert all(json.loads(x['messages'][1]['content'])=={'transcript':RAW,'confirmed_terms':[]} for x in seen)
            assert all(x['thinking']=={'type':'disabled'} and x['stream'] is True and 'response_format' not in x for x in seen)
            snapshot=client.portal.call(ledger,client.app,pair['user']['id'],sid)
            assert len(snapshot['calls'])==calls and all(x['purpose']=='refine' for x in snapshot['calls'])
            assert all(x['provider']=='openai_compatible' and x['model_id']=='deepseek-flash' for x in snapshot['calls'])
            assert [x['status'] for x in snapshot['calls']]==(['failed']*calls if fallback else ['failed']*(calls-1)+['succeeded'])
            for row in snapshot['calls']:
                if row['status']=='succeeded':assert (row['prompt_tokens'],row['completion_tokens'])==(91,37)
                else:assert row['prompt_tokens'] is row['completion_tokens'] is None
            assert snapshot['counts']=={'results':1,'insertions':0,'learning_events':0}
