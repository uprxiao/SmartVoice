"""Actual loopback SSE -> production R2/WS/result/claim/PG, Mock ASR only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
import time

from fastapi.testclient import TestClient
import pytest
import yaml

from app.services.r2_prompt import R2_SYSTEM
from tests.conftest import auth_headers, register_and_login, wait_for_readiness
from tests.cases.test_api_cases import claim, state_of
from tests.integration.test_refiner_factory_protocol import ledger
from tests.test_sessions import create_session, dictation

RAW = '先讨论，不改程序。按钮蓝色，间距12像素，最后给我计划。'
BODY = '先讨论，不改程序。\n\n1. 按钮蓝色。\n2. 间距12像素。\n\n最后给我“计划”🙂。'


def event(content=None, *, finish=None, **delta):
    if content is not None:
        delta['content'] = content
    return ('data: '+json.dumps({'choices': [{'index': 0, 'delta': delta,
        'finish_reason': finish}]}, ensure_ascii=False)+'\n\n').encode()


def payload(mode):
    end = event(finish='stop')+b'data: [DONE]\n\n'
    return {
        'normal': event(BODY)+end,
        'blank': event(' \n ')+end,
        'oversize': event('x'*32769)+end,
        'wrong-type': event(['wrong'])+end,
        'reasoning': event(reasoning_content='fictional hidden reasoning')+event(BODY)+end,
        'tool': event(tool_calls=[{'id':'fictional','type':'function','function':{'name':'fake','arguments':'{}'}}])+end,
        'missing-stop': event(BODY)+b'data: [DONE]\n\n',
        'missing-done': event(BODY)+event(finish='stop'),
        'after-stop': event(BODY)+event(finish='stop')+event('extra')+b'data: [DONE]\n\n',
        'bad-json': b'data: {broken}\n\n',
    }[mode]


@contextmanager
def wire(mode):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            data = b'{"data":[{"id":"deepseek-flash"}]}'
            self.send_response(200); self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)
        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            health = value['messages'][0]['content'].startswith('svi-r2-text-health-v1')
            if not health: seen.append({'request': value, 'received_monotonic': time.monotonic()})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            if mode == 'heartbeats-timeout' and not health:
                self.send_header('Connection', 'close'); self.end_headers()
                try:
                    until = time.monotonic()+12
                    while time.monotonic() < until:
                        self.wfile.write(b': heartbeat\n\n'); self.wfile.flush()
                        time.sleep(.1)
                except (BrokenPipeError, ConnectionResetError): pass
                self.close_connection = True
                return
            data = event('ready')+event(finish='stop')+b'data: [DONE]\n\n' if health else payload(mode)
            self.send_header('Content-Length', str(len(data))); self.end_headers()
            try:
                if mode == 'normal' and not health:
                    for i in range(0, len(data), 3):
                        self.wfile.write(data[i:i+3]); self.wfile.flush()
                else:
                    self.wfile.write(data); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}', seen
    finally: server.shutdown(); server.server_close(); thread.join(2)


@pytest.mark.parametrize('mode', ['normal', 'blank', 'oversize', 'wrong-type', 'reasoning',
    'tool', 'missing-stop', 'missing-done', 'after-stop', 'bad-json', 'heartbeats-timeout'])
def test_all_r2_faults_reach_honest_fallback_claim_gate_release_and_ledger(
    test_database, tmp_path, monkeypatch, mode, record_property,
):
    from app.main import create_app
    monkeypatch.setenv('SVI_INSTANCE_LOCK', '0')
    config = yaml.safe_load(Path('config/app.yaml').read_text())
    with wire(mode) as (url, seen):
        config['llm'].update(provider='openai_compatible', base_url=url, model_id='deepseek-flash',
            thinking='disabled', refine_protocol='ds_r2', max_output_tokens=8192)
        path = tmp_path/'fault.yaml'; path.write_text(yaml.safe_dump(config))
        with TestClient(create_app(path)) as client:
            wait_for_readiness(client)
            cfg = client.app.state.config
            assert cfg.llm.call_timeout_ms == 10000 and cfg.pipeline.stop_to_result_budget_ms == 30000
            assert cfg.llm.automatic_retries_per_attempt == 1
            pair = register_and_login(client, email=f'r2-matrix-{mode}@example.invalid')
            h = auth_headers(pair)
            client.app.state.asr.text = RAW
            session = create_session(client, h, 'r2-matrix-'+mode).json()
            runtime = client.app.state.sessions.runtimes[session['session_id']]
            result = dictation(client, h, session['session_id'])[-1]['result']
            received = time.monotonic()
            fallback = mode != 'normal'
            calls = 1 if mode in ('normal', 'heartbeats-timeout') else 2
            assert len(seen) == calls
            assert result['raw_text'] == RAW and result['refined_text'] == (RAW if fallback else BODY)
            assert result['review_required'] is fallback
            assert result['review_reason'] == ('llm_timeout' if mode == 'heartbeats-timeout' else 'llm_invalid_output' if fallback else None)
            for item in seen:
                request = item['request']
                assert len(request['messages']) == 2 and request['messages'][0]['content'] == R2_SYSTEM
                assert json.loads(request['messages'][1]['content']) == {'transcript': RAW, 'confirmed_terms': []}
                assert request['thinking'] == {'type':'disabled'} and request['max_tokens'] == 8192
                assert request['stream'] is True and 'response_format' not in request
            current = state_of(client, h, session)
            assert current['state'] == 'ready' and current['result'] == result
            assert current['retry_kind'] == ('refine' if fallback else 'none')
            assert not runtime.audio and runtime.asr is None
            snap = client.portal.call(ledger, client.app, pair['user']['id'], session['session_id'])
            assert snap['counts'] == {'results':1, 'insertions':0, 'learning_events':0}
            assert len(snap['calls']) == calls
            for row in snap['calls']:
                assert (row['purpose'], row['provider'], row['model_id']) == ('refine','openai_compatible','deepseek-flash')
                assert row['status'] == ('unknown' if mode == 'heartbeats-timeout' else 'failed' if fallback else 'succeeded')
                assert row['prompt_tokens'] is None and row['completion_tokens'] is None
            unconfirmed = claim(client, h, session, result)
            if fallback:
                assert unconfirmed.status_code == 409 and unconfirmed.json()['error']['code'] == 'REVIEW_REQUIRED'
                assert client.portal.call(ledger, client.app, pair['user']['id'], session['session_id'])['counts']['insertions'] == 0
                assert claim(client, h, session, result, review=True).status_code == 201
            else:
                assert unconfirmed.status_code == 201
            if mode == 'heartbeats-timeout':
                elapsed = received-seen[0]['received_monotonic']
                assert 9.5 <= elapsed <= 30 and 9500 <= result['stop_to_result_ms'] <= 30000
                record_property('actual_heartbeat_call_to_result_seconds', elapsed)
            record_property('r2_fault_control', json.dumps({'mode':mode,'actual_requests':calls,
                'fallback':fallback,'raw_kept':True,'pcm_released':True,'no_native_write':True}))
