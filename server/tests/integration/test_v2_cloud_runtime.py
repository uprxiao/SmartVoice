"""TC-V2-RT-01..14 (environment I): real FastAPI + PostgreSQL + WS runtime,
cloud ASR replaced by the mock Bailian service in its own thread/loop.

Audio time runs on a scaled clock shared by the test client pacing, the
provider and the mock; runtime deadlines stay in wall time (see §局限 in the
run record).
"""
from __future__ import annotations

import asyncio
import struct
import threading
import time
import uuid
from pathlib import Path

import asyncpg
import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.providers.asr.mock import MockAsrProvider
from app.providers.llm.mock import MockLlmProvider
from tests.bailian_audio import SAMPLE_RATE, BLOCK, ScaledClock, expected_text, make_provider, synth
from tests.conftest import (
    PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT, TEST_DB, TEST_URL, auth_headers,
    register_and_login, wait_for_readiness,
)
from tests.fixtures.mock_bailian_server import MockBailianServer

CONFIG_ID = 'b1a2c3d4-0000-4000-8000-00000000c10d'


class ThreadedMock:
    """Mock cloud on its own event loop thread (the app owns TestClient's loop)."""

    def __init__(self, clock, **kw):
        self.clock, self.kw = clock, kw

    def __enter__(self) -> MockBailianServer:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.server = MockBailianServer(self.clock, **self.kw)
        asyncio.run_coroutine_threadsafe(self.server.__aenter__(), self.loop).result(5)
        return self.server

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.server.__aexit__(None, None, None), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def v2_yaml(tmp_path: Path, **audio) -> Path:
    data = yaml.safe_load((SERVER_ROOT / 'config' / 'app.yaml').read_text())
    data['audio'].update({'max_duration_seconds': 360, 'active_pcm_budget_bytes': 1_073_741_824, **audio})
    path = tmp_path / 'app.v2.yaml'
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    return path


@pytest.fixture()
def v2_client(clean_database, monkeypatch, tmp_path):
    path = v2_yaml(tmp_path)
    monkeypatch.setenv('SVI_DATABASE_URL', TEST_URL)
    monkeypatch.setenv('SVI_CONFIG', str(path))
    monkeypatch.setenv('SVI_INSTANCE_LOCK', '0')
    monkeypatch.setenv('SVI_ENVIRONMENT', 'development')
    from app.main import create_app
    with TestClient(create_app(str(path))) as client:
        wait_for_readiness(client)
        yield client


def use_asr(client, provider):
    state = client.app.state
    provider.recorder = state.recorder
    state.asr = provider
    state.sessions.asr_provider = provider


def login(client, n=0):
    # Registration is IP rate limited (5/h); tests register many fictional users.
    client.app.state.register_limiter.reset()
    client.app.state.login_limiter.reset()
    return auth_headers(register_and_login(client, email=f'v2-user{n}@example.com'))


def create(client, headers, request_id=None):
    rid = request_id or str(uuid.uuid4())
    return client.post('/api/v1/sessions', json={'client_request_id': rid, 'learning_enabled': True},
                       headers={**headers, 'Idempotency-Key': rid})


def stream_audio(client, headers, session_id, pcm, clock, *, end=True, reset_clock=True, overflow=False):
    """Send 250 ms frames at their (scaled) real-time instants; return events."""
    events = []
    total = len(pcm) // 2
    with client.websocket_connect(f'/ws/v1/sessions/{session_id}/audio', headers=headers) as ws:
        events.append(ws.receive_json())
        if reset_clock:
            clock.reset()
        t0 = clock.now()
        seq = 0
        for offset in range(0, total, BLOCK):
            count = min(BLOCK, total - offset)
            clock.wait_until(t0 + (offset + count) / SAMPLE_RATE)
            ws.send_bytes(struct.pack('<II', seq, count) + pcm[offset * 2:(offset + count) * 2])
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] == 'audio.ack' and event['seq'] == seq:
                    break
                assert event['type'] in ('asr.partial',), event
            seq += 1
        if overflow:
            ws.send_bytes(struct.pack('<II', seq, 1) + b'\0\0')
            tail = []
            try:
                while True:
                    tail.append(ws.receive_json())
            except WebSocketDisconnect as exc:
                return tail, exc.code
        ws.send_json({'type': 'audio.end', 'last_seq': seq - 1, 'total_samples': total})
        while True:
            event = ws.receive_json()
            events.append(event)
            if event['type'] in ('result.ready', 'error', 'session.empty', 'cancelled'):
                break
    return events


def session(client, headers, sid):
    # Polling a long (5x clock) retry would otherwise hit the 120/min per-login limit.
    client.app.state.authenticated_limiter.reset()
    response = client.get(f'/api/v1/sessions/{sid}', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def wait_terminal(client, headers, sid, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = session(client, headers, sid)
        if body['state'] in ('ready', 'failed', 'empty', 'cancelled'):
            return body
        time.sleep(0.25)
    raise AssertionError('session did not settle')


def sql(query, *args):
    async def run():
        conn = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD,
                                     database=TEST_DB)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()
    return asyncio.run(run())


def runtime_of(client, sid):
    return client.app.state.sessions.runtimes.get(sid)


def alembic(action: str, revision: str) -> None:
    """Run a migration in-process without letting fileConfig disable loggers
    (same preservation as the conftest database fixture)."""
    import logging
    from alembic import command
    from alembic.config import Config as AlembicConfig
    cfg = AlembicConfig(str(SERVER_ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(SERVER_ROOT / 'migrations'))
    states = {lg: lg.disabled for lg in logging.Logger.manager.loggerDict.values() if isinstance(lg, logging.Logger)}
    try:
        getattr(command, action)(cfg, revision)
    finally:
        for lg, disabled in states.items():
            lg.disabled = disabled


# ---- RT-01/02/03 recording limit fixed at creation -----------------------------

def test_rt01_cloud_limit_360(v2_client):
    clock = ScaledClock(20)
    with ThreadedMock(clock) as server:
        use_asr(v2_client, make_provider(server, clock))
        headers = login(v2_client)
        created = create(v2_client, headers)
        assert created.status_code == 201 and created.json()['max_record_seconds'] == 360
        caps = v2_client.get('/api/v1/capabilities', headers=headers).json()
        assert caps['asr']['max_record_seconds'] == 360 and caps['asr']['supports_hints'] is False
        v2_client.post(f"/api/v1/sessions/{created.json()['session_id']}/cancel", headers=headers)


def test_rt02_local_limit_180(v2_client):
    use_asr(v2_client, MockAsrProvider())  # local capability: 180 s
    headers = login(v2_client)
    created = create(v2_client, headers)
    assert created.status_code == 201 and created.json()['max_record_seconds'] == 180
    caps = v2_client.get('/api/v1/capabilities', headers=headers).json()
    assert caps['asr']['max_record_seconds'] == 180


def test_rt03_switch_does_not_truncate_open_session(v2_client):
    clock = ScaledClock(40)
    with ThreadedMock(clock) as server:
        use_asr(v2_client, make_provider(server, clock))
        headers = login(v2_client)
        sid = create(v2_client, headers).json()['session_id']
        use_asr(v2_client, MockAsrProvider())  # admin switched the current ASR to local
        pcm = synth(300)
        events = stream_audio(v2_client, headers, sid, pcm, clock)
        assert events[-1]['type'] == 'result.ready', events[-1]
        assert session(v2_client, headers, sid)['received_samples'] == 300 * SAMPLE_RATE
        headers2 = login(v2_client, 1)
        assert create(v2_client, headers2).json()['max_record_seconds'] == 180


# ---- RT-04/05 360 s boundary and migration -------------------------------------

@pytest.mark.parametrize('overflow', [False, True])
def test_rt04_exact_limit_and_overflow(v2_client, overflow):
    clock = ScaledClock(100)
    with ThreadedMock(clock) as server:
        use_asr(v2_client, make_provider(server, clock))
        headers = login(v2_client)
        sid = create(v2_client, headers).json()['session_id']
        pcm = synth(360)
        if not overflow:
            events = stream_audio(v2_client, headers, sid, pcm, clock)
            assert events[0]['audio']['max_samples'] == 5_760_000
            assert events[-1]['type'] == 'result.ready', events[-1]
            assert sql('select received_samples from sessions where id=$1', sid)[0][0] == 5_760_000
        else:
            events, close_code = stream_audio(v2_client, headers, sid, pcm, clock, overflow=True)
            assert close_code == 4400  # same as V1 over-limit rule
            assert [e['error']['code'] for e in events if e['type'] == 'error'] == ['AUDIO_LIMIT_EXCEEDED']
            assert sql('select received_samples from sessions where id=$1', sid)[0][0] == 5_760_000


def test_rt05_migration_constraint_idempotent(v2_client):
    headers = login(v2_client)
    sid = create(v2_client, headers).json()['session_id']
    v2_client.post(f'/api/v1/sessions/{sid}/cancel', headers=headers)
    before = sql('select id, state, received_samples from sessions')
    definition = "select pg_get_constraintdef(oid) from pg_constraint where conname='ck_sessions_samples'"
    alembic('downgrade', '8c741a2d9e30')
    assert '<= 2880000' in sql(definition)[0][0]
    for _ in range(2):
        alembic('upgrade', 'head')
        assert '<= 5760000' in sql(definition)[0][0]
    assert sql('select id, state, received_samples from sessions') == before
    with pytest.raises(asyncpg.CheckViolationError):
        sql('update sessions set received_samples=5760001 where id=$1', sid)
    sql('update sessions set received_samples=5760000 where id=$1', sid)


# ---- RT-06/07 capacity ----------------------------------------------------------

def test_rt06_pcm_budget_ceiling():
    from app.core.config import Config
    base = {'audio': {'max_duration_seconds': 360, 'active_pcm_budget_bytes': 1_073_741_824}}
    wide = Config.model_validate({**base, 'sessions': {'admission_ceiling': 100, 'initial_max_active': 20}})
    assert wide.session_pcm_reserve == 11_520_000 and wide.effective_admission_ceiling == 93
    assert Config.model_validate(base).effective_admission_ceiling == 20


def test_rt07_provider_concurrency_429(v2_client):
    clock = ScaledClock(20)
    with ThreadedMock(clock) as server:
        use_asr(v2_client, make_provider(server, clock, max_concurrent_sessions=8))
        users = [login(v2_client, n) for n in range(9)]
        created = [create(v2_client, h) for h in users[:8]]
        assert [r.status_code for r in created] == [201] * 8
        rid = str(uuid.uuid4())
        denied = create(v2_client, users[8], rid)
        assert denied.status_code == 429, denied.text
        assert denied.json()['error']['code'] == 'CAPACITY_EXCEEDED'
        assert denied.json()['error']['details']['reason'] == 'provider_limit'
        assert denied.headers['Retry-After'] == '1'
        assert sql('select count(*) from sessions where client_request_id=$1', rid)[0][0] == 0
        v2_client.post(f"/api/v1/sessions/{created[0].json()['session_id']}/cancel", headers=users[0])
        assert create(v2_client, users[8], rid).status_code == 201  # same key not consumed
        for h, r in zip(users[1:8], created[1:]):
            v2_client.post(f"/api/v1/sessions/{r.json()['session_id']}/cancel", headers=h)


# ---- RT-08/09 hints flag and attempt/model_calls metadata ----------------------

def test_rt08_cloud_hints_not_applied_but_refine_terms_kept(v2_client, monkeypatch):
    seen = []
    original = MockLlmProvider.generate

    async def spy(self, request, deadline):
        if request.purpose == 'refine':
            seen.append(request.input)
        return await original(self, request, deadline)

    monkeypatch.setattr(MockLlmProvider, 'generate', spy)
    clock = ScaledClock(20)
    with ThreadedMock(clock, text_fn=lambda a, s, e: '我们用React和Docker写TypeScript。') as server:
        use_asr(v2_client, make_provider(server, clock))
        headers = login(v2_client)
        for term in ('React', 'Docker', 'TypeScript'):
            r = v2_client.post('/api/v1/dictionary/terms', json={'term': term, 'aliases': [], 'scope': 'default'},
                               headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
            assert r.status_code in (200, 201), r.text
        sid = create(v2_client, headers).json()['session_id']
        events = stream_audio(v2_client, headers, sid, synth(5), clock)
    assert events[-1]['type'] == 'result.ready', events[-1]
    hints, applied = sql('select asr_hints, asr_hints_applied from sessions where id=$1', sid)[0]
    assert applied is False and hints  # candidates recorded, not sent to cloud
    terms = {t['term'] if isinstance(t, dict) else t for t in seen[-1]['confirmed_terms']}
    assert {'React', 'Docker', 'TypeScript'} <= terms
    assert 'corpus' not in str(server.connections[0].session_updates)


def test_rt09_attempt_and_model_call_metadata(v2_client):
    clock = ScaledClock(40)
    with ThreadedMock(clock, drop_after_commit={1: 0.05}) as server:
        use_asr(v2_client, make_provider(server, clock, provider_config_id=CONFIG_ID))
        headers = login(v2_client)
        sid = create(v2_client, headers).json()['session_id']
        events = stream_audio(v2_client, headers, sid, synth(75, ((27.0, 0.6), (58.0, 0.6))), clock)
    assert events[-1]['type'] == 'result.ready', events[-1]
    provider_id, reconnects, resend = sql(
        'select asr_provider_id, asr_reconnects, asr_resend_ms from session_attempts where session_id=$1', sid)[0]
    assert provider_id == 'bailian_realtime' and reconnects == 1 and resend > 0
    row = sql("select provider, purpose, status, provider_config_id, segments, reconnects, processed_audio_samples "
              "from model_calls where session_id=$1 and purpose='asr_finish'", sid)
    assert len(row) == 1
    assert tuple(row[0]) == ('bailian_realtime', 'asr_finish', 'succeeded', CONFIG_ID, 3, 1, 75 * SAMPLE_RATE)


# ---- RT-10/11/12 failure, resume retry, TTL, provider change -------------------

def fail_at_70(v2_client, clock, server, seconds=120):
    server.refuse_after, server.pending_drops = 70.0, [70.0]
    headers = login(v2_client)
    sid = create(v2_client, headers).json()['session_id']
    events = stream_audio(v2_client, headers, sid, synth(seconds), clock)
    return headers, sid, events


def test_rt10_retry_resends_only_unfinished(v2_client):
    # 5x (not 20x): per-block send overhead is magnified by the clock factor and
    # would otherwise eat the replay deadline's margin in the test itself.
    clock = ScaledClock(5)
    with ThreadedMock(clock) as server:
        use_asr(v2_client, make_provider(server, clock))
        headers, sid, events = fail_at_70(v2_client, clock, server)
        assert events[-1]['type'] == 'error'
        failed = session(v2_client, headers, sid)
        assert (failed['state'], failed['error_code'], failed['retry_kind']) == ('failed', 'ASR_FAILED', 'asr')
        lease = runtime_of(v2_client, sid).audio_lease
        completed_end = lease.resume.end_sample
        assert completed_end == 35 * SAMPLE_RATE
        server.refuse_after, server.pending_drops = None, []
        connections_before = len(server.connections)
        manager = v2_client.app.state.sessions
        budgets = []
        original = manager._asr_operation_deadline

        def spy(runtime):
            value = original(runtime)
            if runtime.replay_retry and runtime.end_accepted_at is not None:
                budgets.append((value - runtime.end_accepted_at, runtime.deadline - runtime.end_accepted_at))
            return value

        manager._asr_operation_deadline = spy
        retry_started = clock.now()
        response = v2_client.post(f'/api/v1/sessions/{sid}/retry', json={'retry_kind': 'asr'},
                                  headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
        assert response.status_code == 202, response.text
        final = wait_terminal(v2_client, headers, sid)
        retry_audio_s = clock.now() - retry_started
    assert final['state'] == 'ready', final
    conn = server.connections[connections_before]
    assert conn.first_marker * BLOCK == completed_end
    assert sum(n for _, _, n in conn.appends) == 120 * SAMPLE_RATE - completed_end
    expected = expected_text(0, completed_end) + ''.join(
        expected_text(a, b) for a, b in [(completed_end, 70 * SAMPLE_RATE), (70 * SAMPLE_RATE, 105 * SAMPLE_RATE),
                                         (105 * SAMPLE_RATE, 120 * SAMPLE_RATE)])
    assert final['result']['raw_text'] == expected
    # T12 (decided 2026-10-28): the retry ASR deadline is replay time + 10 s
    # (85 s unfinished audio at 2× → 42.5 + 10 = 52.5 s), not capped at 25 s, and
    # the attempt budget grows by the replay time; the replay must finish within it.
    replay_s = (120 * SAMPLE_RATE - completed_end) / SAMPLE_RATE / 2.0
    asr_budget, attempt_budget = budgets[-1]
    assert abs(asr_budget - (replay_s + 10)) <= 1.0, budgets
    assert attempt_budget >= replay_s + 10 + 30 - 1.0, budgets
    # The ASR replay itself (first to last appended block on the retry connection,
    # in audio time) fits the deadline. retry_audio_s also contains HTTP, refine and
    # 50 ms polling magnified by the 20x test clock, so it is reported, not judged.
    replay_span = conn.appends[-1][0] - conn.appends[0][0]
    print(f'RT-10 replay span {replay_span:.1f} s, end-to-end {retry_audio_s:.1f} s (audio time)')
    assert replay_span <= replay_s + 10, f'retry replay took {replay_span:.1f} s of audio time'


def test_rt11_resume_and_pcm_cleared_by_ttl(clean_database, monkeypatch, tmp_path, caplog):
    path = v2_yaml(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['retry'] = {'audio_ttl_seconds': 3}
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv('SVI_DATABASE_URL', TEST_URL)
    monkeypatch.setenv('SVI_INSTANCE_LOCK', '0')
    from app.main import create_app
    clock = ScaledClock(40)
    with TestClient(create_app(str(path))) as client, ThreadedMock(clock) as server:
        wait_for_readiness(client)
        use_asr(client, make_provider(server, clock))
        headers, sid, _ = fail_at_70(client, clock, server)
        runtime = runtime_of(client, sid)
        lease = runtime.audio_lease
        assert lease.resume is not None and runtime.audio
        expires = lease.expires_at
        time.sleep(max(0, expires - time.time()) + 1.05)  # D + 1 s
        assert not runtime.audio and lease.resume is None and lease.status == 'expired'
        response = client.post(f'/api/v1/sessions/{sid}/retry', json={'retry_kind': 'asr'},
                               headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
        assert response.status_code == 409
        assert response.json()['error']['details']['reason'] == 'audio_expired'
    dumped = '\n'.join(str(tuple(r)) for t in ('sessions', 'session_attempts', 'results', 'model_calls')
                       for r in sql(f'select * from {t}'))
    assert 'seg[' not in dumped
    assert 'seg[' not in '\n'.join(r.getMessage() for r in caplog.records if not r.name.startswith('tests.'))


@pytest.mark.parametrize('seconds,switchable', [(360, False), (120, True)])
def test_rt12_retry_after_provider_change(v2_client, seconds, switchable):
    clock = ScaledClock(40)
    with ThreadedMock(clock) as server:
        use_asr(v2_client, make_provider(server, clock))
        headers, sid, _ = fail_at_70(v2_client, clock, server, seconds)
        use_asr(v2_client, MockAsrProvider())  # switched to local 180 s
        body = session(v2_client, headers, sid)
        response = v2_client.post(f'/api/v1/sessions/{sid}/retry', json={'retry_kind': 'asr'},
                                  headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
        if not switchable:
            assert (body['retry_kind'], body['retry_unavailable_reason']) == ('none', 'provider_changed')
            assert response.status_code == 409
            assert response.json()['error']['code'] == 'RETRY_UNAVAILABLE'
            assert response.json()['error']['details']['reason'] == 'provider_changed'
        else:
            assert body['retry_kind'] == 'asr' and response.status_code == 202, response.text
            final = wait_terminal(v2_client, headers, sid)
            assert final['state'] == 'ready' and final['result']['raw_text']


# ---- RT-13/14 deadlines and configuration --------------------------------------

def test_rt13_cloud_and_local_finish_deadline():
    from types import SimpleNamespace
    from app.core.config import Config
    from app.services.session_runtime import SessionManager
    manager = SessionManager.__new__(SessionManager)
    manager.config = Config.model_validate({})
    cloud = SimpleNamespace(asr_caps=SimpleNamespace(replay_rate=2.0), deadline=None, end_accepted_at=100.0,
                            asr=SimpleNamespace(drain_estimate_s=lambda: 15.0), replay_retry=False)
    assert manager._asr_operation_deadline(cloud) == 120.0  # min(25, max(10, 15 + 5))
    cloud.asr = SimpleNamespace(drain_estimate_s=lambda: 30.0)
    assert manager._asr_operation_deadline(cloud) == 125.0
    cloud.asr = SimpleNamespace(drain_estimate_s=lambda: 0.0)
    assert manager._asr_operation_deadline(cloud) == 110.0
    local = SimpleNamespace(asr_caps=SimpleNamespace(replay_rate=None), deadline=None, end_accepted_at=100.0, asr=None, replay_retry=False)
    assert manager._asr_operation_deadline(local) == 110.0
    # T12: a cloud retry replaying 85 s at 2× gets 42.5 + 10 s, beyond the 25 s cap.
    retry = SimpleNamespace(asr_caps=SimpleNamespace(replay_rate=2.0), deadline=None, end_accepted_at=100.0,
                            asr=SimpleNamespace(drain_estimate_s=lambda: 42.5), replay_retry=True)
    assert manager._asr_operation_deadline(retry) == 152.5


def test_rt13_backlog_at_stop_uses_extended_deadline(v2_client):
    clock = ScaledClock(10)
    with ThreadedMock(clock, drop_at=(30.0,), refuse_between=(30.0, 45.0)) as server:
        use_asr(v2_client, make_provider(server, clock))
        headers = login(v2_client)
        sid = create(v2_client, headers).json()['session_id']
        manager = v2_client.app.state.sessions
        seen = []
        original = manager._asr_operation_deadline

        def spy(runtime):
            value = original(runtime)
            if runtime.end_accepted_at is not None:
                seen.append(value - runtime.end_accepted_at)
            return value

        manager._asr_operation_deadline = spy
        events = stream_audio(v2_client, headers, sid, synth(60), clock)
    assert events[-1]['type'] in ('result.ready', 'error')
    # Stop with roughly 30 s of backlog (≈15 s at 2×): budget = 15 + 5 ≈ 20 s, never > 25.
    assert seen and 15 <= max(seen) <= 25, seen


def test_rt14_finish_timeout_validation():
    from pydantic import ValidationError
    from app.core.config import Config
    Config.model_validate({'asr': {'provider': 'bailian_realtime', 'finish_timeout_ms': 25_000,
                                   'remote_base_url': 'wss://ws-a.maas.aliyuncs.com'}})
    with pytest.raises(ValidationError):
        Config.model_validate({'asr': {'provider': 'qwen3_mlx', 'finish_timeout_ms': 25_000}})
    with pytest.raises(ValidationError):
        Config.model_validate({'asr': {'provider': 'bailian_realtime', 'params': {'catchup_rate': 3.0}}})


# ---- PROV-06 (runtime half): all-empty cloud segments → empty, no refine ----------

def test_prov06_runtime_empty_without_refine(v2_client, monkeypatch):
    calls = []
    original = MockLlmProvider.generate

    async def spy(self, request, deadline):
        calls.append(request.purpose)
        return await original(self, request, deadline)

    monkeypatch.setattr(MockLlmProvider, 'generate', spy)
    from tests.bailian_audio import silence
    clock = ScaledClock(20)
    with ThreadedMock(clock, text_fn=lambda audio, a, b: '') as server:
        use_asr(v2_client, make_provider(server, clock))
        headers = login(v2_client)
        sid = create(v2_client, headers).json()['session_id']
        events = stream_audio(v2_client, headers, sid, silence(12), clock)
    assert events[-1]['type'] == 'session.empty', events[-1]
    assert session(v2_client, headers, sid)['state'] == 'empty'
    assert calls == []


def test_real_cloud_silence_filler_still_ends_empty(v2_client, monkeypatch):
    """Real Bailian returns "嗯。" for pure silence (2026-10-28 smoke); the runtime
    must still take the empty path without refine (PROV-06)."""
    calls = []
    original = MockLlmProvider.generate

    async def spy(self, request, deadline):
        calls.append(request.purpose)
        return await original(self, request, deadline)

    monkeypatch.setattr(MockLlmProvider, 'generate', spy)
    from tests.bailian_audio import silence
    clock = ScaledClock(20)
    with ThreadedMock(clock, text_fn=lambda audio, a, b: '嗯。') as server:
        use_asr(v2_client, make_provider(server, clock))
        headers = login(v2_client)
        sid = create(v2_client, headers).json()['session_id']
        events = stream_audio(v2_client, headers, sid, silence(12), clock)
    assert events[-1]['type'] == 'session.empty', events[-1]
    assert calls == []
