"""Protocol, recovery and storage guarantees; no models, sockets or microphone."""

import asyncio
import sqlite3
import struct
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from smartdd_voice.api import create_app
from smartdd_voice.config import Config
from smartdd_voice.dictionary import Conflict, Dictionary
from smartdd_voice.session import Session


class FakeEngine:
    model_id = 'edge-test-asr'

    def __init__(self):
        self.calls = []
        self.fail_once = False

    async def prepare(self):
        pass

    async def decode(self, data, prompt):
        self.calls.append((data, prompt))
        await asyncio.sleep(.01)
        if self.fail_once:
            self.fail_once = False
            raise ValueError('injected decoder failure')
        seconds = len(data) / 32000
        return {'words': [(0, seconds, '请保留 cloud 和 42 个文件。')],
                'segments': [seconds], 'seconds': .01}

    def close(self):
        pass


class FakeRefiner:
    digest = 'edge-test-refiner'

    def __init__(self):
        self.calls = 0
        self.delay = 0

    async def prepare(self):
        pass

    async def refine(self, text, terms):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return text, None

    async def classify(self, *args):
        return 'term'

    async def close(self):
        pass


def config_for(tmp_path, **overrides):
    return Config.model_validate({
        'server': {'auth_token_file': tmp_path / 'token'},
        'dictionary': {'sqlite_path': tmp_path / 'dictionary.sqlite3'},
        **overrides,
    })


def frame(seq, samples):
    return struct.pack('<II', seq, samples) + struct.pack('<h', 20) * samples


@contextmanager
def running_client(config, engine=None, refiner=None):
    engine, refiner = engine or FakeEngine(), refiner or FakeRefiner()
    app = create_app(config, engine=engine, refiner=refiner)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + config.server.auth_token_file.read_text()
        deadline = time.monotonic() + 2
        while not client.get('/v1/capabilities').json()['ready']:
            assert time.monotonic() < deadline, 'fake service did not become ready'
            time.sleep(.005)
        yield client, app.state.service, engine, refiner


def new_session(client, request_id='edge-test-request'):
    response = client.post('/v1/sessions', json={'client_request_id': request_id})
    assert response.status_code == 200, response.text
    return response.json()['session_id']


def wait_state(client, sid, states):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f'/v1/sessions/{sid}')
        assert response.status_code == 200, response.text
        value = response.json()
        if value['state'] in states:
            return value
        time.sleep(.005)
    pytest.fail(f'session did not reach {states}: {value}')


def finish_short(client, sid, samples=23):
    with client.websocket_connect(f'/v1/sessions/{sid}/audio', headers=client.headers) as ws:
        assert ws.receive_json()['type'] == 'ready'
        ws.send_bytes(frame(0, samples))
        assert ws.receive_json()['type'] == 'audio.ack'
        ws.send_json({'type': 'audio.end', 'last_seq': 0, 'total_samples': samples})
        while (event := ws.receive_json())['type'] != 'result.ready':
            assert event['type'] != 'error', event
        return event['result']


def disconnect_and_wait(ws):
    # TestClient cancels its ASGI scope immediately on context exit. Explicitly
    # deliver the disconnect and await server close to exercise real cleanup.
    ws.close()
    while ws.receive()['type'] != 'websocket.close':
        pass


def test_zero_audio_fails_without_model_or_generated_text(tmp_path):
    with running_client(config_for(tmp_path)) as (client, service, engine, refiner):
        sid = new_session(client)
        with client.websocket_connect(f'/v1/sessions/{sid}/audio', headers=client.headers) as ws:
            ws.receive_json()
            ws.send_json({'type': 'audio.end', 'last_seq': -1, 'total_samples': 0})
            assert ws.receive_json()['type'] == 'error'
        result = client.get(f'/v1/sessions/{sid}').json()
        assert result['state'] == 'failed' and result['result'] is None
        assert engine.calls == [] and refiner.calls == 0
        assert not service.sessions[sid].audio


@pytest.mark.parametrize('samples', [1, 23, 3999])
def test_sub_chunk_tail_is_decoded_without_padding_or_loss(tmp_path, samples):
    with running_client(config_for(tmp_path)) as (client, service, engine, refiner):
        sid = new_session(client)
        result = finish_short(client, sid, samples)
        assert len(engine.calls) == 1
        assert engine.calls[0][0] == frame(0, samples)[8:]
        assert '42' in result['text'] and refiner.calls == 1
        assert client.get(f'/v1/sessions/{sid}').json()['decoded_samples'] == samples
        assert service.sessions[sid].audio == b''


def test_duplicate_end_cannot_duplicate_finalization(tmp_path):
    async def scenario():
        config = config_for(tmp_path)
        dictionary = Dictionary(config.dictionary.sqlite_path)
        engine, refiner = FakeEngine(), FakeRefiner()
        session = Session('duplicate-end', config, engine, refiner, dictionary, 'epoch')
        session.accept(frame(0, 37))
        session.end(0, 37)
        session.end(0, 37)
        await session.run()
        result_id = session.result['result_id']
        session.end(0, 37)
        events = []
        while not session.events.empty():
            events.append(session.events.get_nowait()['type'])
        assert events.count('result.ready') == 1
        assert len(engine.calls) == 1 and refiner.calls == 1
        assert session.result['result_id'] == result_id
        assert len([t for t in session.timeline if t['event'] == 'audio.end']) == 1
        dictionary.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('last_seq,total_samples', [(0, 24), (1, 23), (-1, 23)])
def test_end_must_account_for_every_acknowledged_sample(tmp_path, last_seq, total_samples):
    with running_client(config_for(tmp_path)) as (client, service, _, refiner):
        sid = new_session(client)
        with client.websocket_connect(f'/v1/sessions/{sid}/audio', headers=client.headers) as ws:
            ws.receive_json()
            ws.send_bytes(frame(0, 23))
            ws.receive_json()
            ws.send_json({'type': 'audio.end', 'last_seq': last_seq, 'total_samples': total_samples})
            assert ws.receive_json()['type'] == 'error'
        state = wait_state(client, sid, {'failed'})
        assert state['result'] is None and refiner.calls == 0
        assert not service.sessions[sid].audio


@pytest.mark.parametrize('bad_frame', [frame(0, 0), frame(1, 20), struct.pack('<IIh', 0, 2, 20)])
def test_invalid_binary_frames_fail_closed(tmp_path, bad_frame):
    with running_client(config_for(tmp_path)) as (client, service, _, _):
        sid = new_session(client)
        with client.websocket_connect(f'/v1/sessions/{sid}/audio', headers=client.headers) as ws:
            ws.receive_json()
            ws.send_bytes(bad_frame)
            assert ws.receive_json()['type'] == 'error'
        assert service.sessions[sid].result is None
        assert not service.sessions[sid].audio


@pytest.mark.parametrize('control', [[], None, 42, 'audio.end'])
def test_non_object_control_frames_return_protocol_error(tmp_path, control):
    with running_client(config_for(tmp_path)) as (client, service, _, _):
        sid = new_session(client)
        with client.websocket_connect(f'/v1/sessions/{sid}/audio', headers=client.headers) as ws:
            ws.receive_json()
            ws.send_json(control)
            assert ws.receive_json()['type'] == 'error'
        assert service.sessions[sid].result is None


def test_disconnect_before_end_discards_audio_and_requires_new_session(tmp_path):
    with running_client(config_for(tmp_path)) as (client, service, _, _):
        sid = new_session(client)
        with client.websocket_connect(f'/v1/sessions/{sid}/audio', headers=client.headers) as ws:
            ws.receive_json()
            ws.send_bytes(frame(0, 23))
            ws.receive_json()
            disconnect_and_wait(ws)
        state = wait_state(client, sid, {'cancelled'})
        assert state['result'] is None and not service.sessions[sid].audio
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f'/v1/sessions/{sid}/audio', headers=client.headers):
                pass
        assert new_session(client, 'after-disconnect') != sid


def test_disconnect_after_end_recovers_same_result_by_get(tmp_path):
    refiner = FakeRefiner()
    refiner.delay = .1
    with running_client(config_for(tmp_path), refiner=refiner) as (client, service, engine, _):
        sid = new_session(client)
        with client.websocket_connect(f'/v1/sessions/{sid}/audio', headers=client.headers) as ws:
            ws.receive_json()
            ws.send_bytes(frame(0, 23))
            ws.receive_json()
            ws.send_json({'type': 'audio.end', 'last_seq': 0, 'total_samples': 23})
            wait_state(client, sid, {'finalizing', 'refining'})
            disconnect_and_wait(ws)
        result = wait_state(client, sid, {'ready'})['result']
        assert client.get(f'/v1/sessions/{sid}').json()['result'] == result
        assert len(engine.calls) == 1 and refiner.calls == 1
        assert not service.sessions[sid].audio
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f'/v1/sessions/{sid}/audio', headers=client.headers):
                pass


def test_decoder_failure_can_retry_only_retained_complete_audio(tmp_path):
    engine = FakeEngine()
    engine.fail_once = True
    with running_client(config_for(tmp_path), engine=engine) as (client, service, _, _):
        sid = new_session(client)
        with client.websocket_connect(f'/v1/sessions/{sid}/audio', headers=client.headers) as ws:
            ws.receive_json()
            ws.send_bytes(frame(0, 53))
            ws.receive_json()
            ws.send_json({'type': 'audio.end', 'last_seq': 0, 'total_samples': 53})
            assert ws.receive_json()['type'] == 'error'
        assert service.sessions[sid].audio == frame(0, 53)[8:]
        retried = client.post(f'/v1/sessions/{sid}/retry', json={'client_request_id': 'retry-complete-audio'})
        assert retried.status_code == 200, retried.text
        second_sid = retried.json()['session_id']
        assert second_sid != sid
        assert wait_state(client, second_sid, {'ready'})['result']['text']
        assert engine.calls[0][0] == engine.calls[1][0]
        assert client.post(f'/v1/sessions/{second_sid}/retry', json={'client_request_id': 'no-retry-ready'}).status_code == 409


@pytest.mark.parametrize('stage', ['decode', 'refine'])
def test_cancel_during_await_prevents_late_result(tmp_path, stage):
    async def scenario():
        config = config_for(tmp_path)
        dictionary = Dictionary(config.dictionary.sqlite_path)
        entered, release = asyncio.Event(), asyncio.Event()
        engine, refiner = FakeEngine(), FakeRefiner()
        original = engine.decode if stage == 'decode' else refiner.refine

        async def blocked(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        if stage == 'decode':
            engine.decode = blocked
        else:
            refiner.refine = blocked
        session = Session('cancel-await', config, engine, refiner, dictionary, 'epoch')
        session.accept(frame(0, 23))
        session.end(0, 23)
        task = asyncio.create_task(session.run())
        await asyncio.wait_for(entered.wait(), 1)
        session.cancel()
        release.set()
        await asyncio.wait_for(task, 1)
        events = []
        while not session.events.empty():
            events.append(session.events.get_nowait()['type'])
        assert session.state == 'cancelled' and session.result is None
        assert not session.audio and 'result.ready' not in events
        dictionary.close()
    asyncio.run(scenario())


def test_service_restart_invalidates_results_but_keeps_dictionary(tmp_path):
    config = config_for(tmp_path)
    with running_client(config) as (client, _, _, _):
        epoch = client.get('/v1/capabilities').json()['service_epoch']
        sid = new_session(client)
        finish_short(client, sid)
        saved = client.post('/v1/dictionary/terms', json={'term': 'SmartDD', 'aliases': ['smart dd']}).json()
    with running_client(config) as (client, _, _, _):
        assert client.get('/v1/capabilities').json()['service_epoch'] != epoch
        assert client.get(f'/v1/sessions/{sid}').status_code == 410
        assert client.get('/v1/dictionary/terms').json()['terms'] == [saved]


def test_retention_limit_expires_old_result_without_touching_new_audio(tmp_path):
    config = config_for(tmp_path, storage={'max_retained_sessions': 1})
    with running_client(config) as (client, service, _, _):
        first = new_session(client)
        finish_short(client, first)
        second = new_session(client, 'next-retained-session')
        assert client.get(f'/v1/sessions/{first}').status_code == 410
        assert set(service.sessions) == {second}
        assert finish_short(client, second)['text']


@pytest.mark.parametrize('existing', [False, True])
def test_learning_event_failure_rolls_back_term_and_dictionary_version(tmp_path, existing):
    path = tmp_path / 'atomic.sqlite3'
    dictionary = Dictionary(path)
    if existing:
        dictionary.put('claw', ['克劳'])
        dictionary.learn('older-success', 'claud', 'claw', left='使用 ', right=' 工具')
    before, version = dictionary.list(), dictionary.version
    event_count = dictionary.db.execute('SELECT COUNT(*) FROM learning_events').fetchone()[0]
    dictionary.db.execute('''CREATE TEMP TRIGGER reject_learning BEFORE INSERT ON learning_events
        BEGIN SELECT RAISE(ABORT, 'injected event write failure'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        dictionary.learn('atomic-correction', 'cloud', 'claw', left='配置 ', right=' 智能代理')
    assert dictionary.list() == before and dictionary.version == version
    assert dictionary.db.execute('SELECT COUNT(*) FROM learning_events').fetchone()[0] == event_count
    dictionary.close()
    dictionary = Dictionary(path)
    assert dictionary.list() == before and dictionary.version == version
    dictionary.close()


def test_mixed_chinese_ascii_term_boundaries_do_not_match_inside_ascii_words(tmp_path):
    dictionary = Dictionary(tmp_path / 'terms.sqlite3')
    term = dictionary.put('claw', ['cloud'])
    for text in ['用cloud来开发', 'CLOUD，保留其他内容', '我说claw工具']:
        assert [t['id'] for t in dictionary.relevant(text)] == [term['id']]
    for text in ['cloudy', 'mycloud', 'cloud_name', 'cloud2', 'claws']:
        assert dictionary.relevant(text) == []
    dictionary.close()


def test_stale_edit_delete_and_undo_do_not_destroy_newer_term(tmp_path):
    dictionary = Dictionary(tmp_path / 'version.sqlite3')
    term = dictionary.put('claw', ['克劳'])
    learned = dictionary.learn('versioned-correction', 'cloud', 'claw')
    current = learned['term']
    with pytest.raises(Conflict):
        dictionary.put('old', [], term_id=term['id'], version=term['version'])
    with pytest.raises(Conflict):
        dictionary.delete(term['id'], term['version'])
    assert dictionary.get(term['id']) == current
    edited = dictionary.put('claw', ['手动保留'], term_id=term['id'], version=current['version'])
    with pytest.raises(Conflict):
        dictionary.undo(learned['event_id'])
    assert dictionary.get(term['id']) == edited
    dictionary.close()


def test_undo_and_duplicate_learning_remain_idempotent_after_restart(tmp_path):
    path = tmp_path / 'undo.sqlite3'
    dictionary = Dictionary(path)
    learned = dictionary.learn('persistent-correction', 'cloud', 'claw')
    version = dictionary.version
    assert dictionary.learn('persistent-correction', 'cloud', 'claw')['event_id'] == learned['event_id']
    assert dictionary.version == version
    dictionary.undo(learned['event_id'])
    after_undo_version = dictionary.version
    dictionary.close()
    dictionary = Dictionary(path)
    assert dictionary.undo(learned['event_id']) == {'undone': True}
    assert dictionary.learn('persistent-correction', 'cloud', 'claw')['undone'] is True
    assert dictionary.version == after_undo_version and dictionary.list() == []
    dictionary.close()


def test_learning_contexts_are_bounded_persistent_and_restored_by_undo(tmp_path):
    path = tmp_path / 'context.sqlite3'
    dictionary = Dictionary(path)
    left, right = '前' * 100, '后' * 100
    for index in range(4):
        if index == 3:
            before = dictionary.list()[0]
        learned = dictionary.learn(f'context-{index}', 'cloud', 'claw',
                                   left=f'{left}{index}', right=f'{index}{right}')
    term = learned['term']
    assert len(term['contexts']) == 3
    assert [c['left'][-1] for c in term['contexts']] == ['1', '2', '3']
    assert all(len(c['left']) == 80 and len(c['right']) == 80 for c in term['contexts'])
    assert all(c['alias'] == 'cloud' for c in term['contexts'])
    dictionary.close()
    dictionary = Dictionary(path)
    assert dictionary.get(term['id'])['contexts'] == term['contexts']
    dictionary.undo(learned['event_id'])
    restored = dictionary.get(term['id'])
    assert restored['contexts'] == before['contexts']
    assert restored['aliases'] == before['aliases']
    assert restored['version'] > term['version']
    dictionary.close()


@pytest.mark.parametrize('edit', ['remove_alias', 'rename_term'])
def test_manual_edit_discards_obsolete_learning_context(tmp_path, edit):
    dictionary = Dictionary(tmp_path / 'context-edit.sqlite3')
    learned = dictionary.learn('context-edit', 'cloud', 'claw', left='用 ', right=' 来编程')
    term = learned['term']
    assert term['contexts']
    edited = dictionary.put('claw' if edit == 'remove_alias' else '新工具',
                            [] if edit == 'remove_alias' else ['cloud'],
                            term_id=term['id'], version=term['version'])
    assert edited['contexts'] == []
    with pytest.raises(Conflict):
        dictionary.undo(learned['event_id'])
    dictionary.close()


def test_schema_upgrade_preserves_legacy_terms_and_versions(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''
          CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
          INSERT INTO meta VALUES ('version',7);
          CREATE TABLE terms (
            id TEXT PRIMARY KEY, term TEXT NOT NULL, norm TEXT NOT NULL, aliases TEXT NOT NULL,
            scope TEXT NOT NULL, source TEXT NOT NULL, version INTEGER NOT NULL,
            updated_at REAL NOT NULL, UNIQUE(norm,scope));
          INSERT INTO terms VALUES ('legacy-id','claw','claw','["cloud"]','default','manual',3,1.0);
        ''')
    dictionary = Dictionary(path)
    term = dictionary.get('legacy-id')
    assert term['aliases'] == ['cloud'] and term['version'] == 3
    assert term['contexts'] == [] and dictionary.version == 7
    learned = dictionary.learn('after-migration', 'claud', 'claw', left='使用 ', right=' 工具')
    assert learned['term']['source'] == 'manual'
    dictionary.undo(learned['event_id'])
    assert dictionary.get('legacy-id')['aliases'] == ['cloud']
    assert dictionary.get('legacy-id')['contexts'] == []
    dictionary.close()
