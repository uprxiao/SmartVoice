"""TC-V2-PROV-01..18: bailian_realtime against the mock cloud (environment M).

Audio time is scaled (``ScaledClock``); tolerances are stated in audio seconds
exactly as in docs/04-test/test-cases-v2.md.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
import websockets

from app.core.egress import EgressPolicy
from app.providers.asr import bailian_conn
from app.providers.asr.bailian_realtime import BailianRealtimeConfig, BailianRealtimeProvider
from app.providers.asr.base import ProviderError
from tests.bailian_audio import (
    BLOCK, SAMPLE_RATE, TEST_KEY, ScaledClock, expected_text, feed_realtime, loop_deadline, make_provider,
    options, silence, synth,
)
from tests.fixtures.mock_bailian_server import MOCK_LOGGER, MockBailianServer, marker_of

TONE_75 = ((27.0, 0.6), (33.0, 0.6), (58.0, 0.6))


async def run_session(provider, pcm, clock, *, deadline_s=30, fed=None, **open_kw):
    session = await provider.open(options(**open_kw))
    partials = []

    async def collect():
        async for p in session.partials():
            partials.append(p)

    collector = asyncio.create_task(collect())
    await feed_realtime(session, pcm, clock, fed=fed)
    final = await session.finish(len(pcm) // 2, loop_deadline(deadline_s))
    await asyncio.wait_for(collector, 5)
    return session, final, partials


def segment_bounds(final):
    return [(s.start_sample, s.end_sample) for s in final.segments]


# ---- PROV-01..03 segmentation -------------------------------------------------

async def test_prov01_cut_after_first_quiet_block():
    clock = ScaledClock(20)
    pcm = synth(75, TONE_75)
    async with MockBailianServer(clock) as server:
        _, final, _ = await run_session(make_provider(server, clock), pcm, clock)
    cut1, cut2 = int(27.25 * SAMPLE_RATE), int(58.25 * SAMPLE_RATE)
    assert segment_bounds(final) == [(0, cut1), (cut1, cut2), (cut2, len(pcm) // 2)]
    assert all(end - start >= 10 * SAMPLE_RATE for start, end in segment_bounds(final))
    conn = server.connections[0]
    assert conn.commits == segment_bounds(final)
    assert [s.text for s in final.segments] == [expected_text(a, b) for a, b in segment_bounds(final)]


async def test_prov02_hard_cut_and_no_holding(monkeypatch):
    clock = ScaledClock(4)
    pcm = synth(100)
    fed, sent = {}, {}
    real_append = bailian_conn.append_message

    def recording_append(chunk):
        sent[marker_of(chunk)] = clock.now()
        return real_append(chunk)

    monkeypatch.setattr(bailian_conn, 'append_message', recording_append)
    async with MockBailianServer(clock) as server:
        _, final, _ = await run_session(make_provider(server, clock), pcm, clock, fed=fed)
    bounds = segment_bounds(final)
    assert bounds[:2] == [(0, 35 * SAMPLE_RATE), (35 * SAMPLE_RATE, 70 * SAMPLE_RATE)]
    # No holding: every block is sent within ±50 ms of arriving at the provider.
    late = [(marker, round(sent[marker] - fed[marker], 3)) for marker in fed if marker < 400]
    assert len(late) == 400
    assert max(abs(d) for _, d in late) <= 0.05, sorted(late, key=lambda x: -abs(x[1]))[:5]


async def test_prov03_minimum_segment_length():
    clock = ScaledClock(20)
    pcm = synth(40, ((3.0, 0.6), (8.0, 0.6), (26.0, 0.6)))
    async with MockBailianServer(clock) as server:
        _, final, _ = await run_session(make_provider(server, clock), pcm, clock)
    assert segment_bounds(final)[0] == (0, int(26.25 * SAMPLE_RATE))


# ---- PROV-04..06 partials / final / no speech ---------------------------------

async def test_prov04_partials_contract():
    clock = ScaledClock(20)
    pcm = synth(75, TONE_75)
    async with MockBailianServer(clock, revise_partials=True) as server:
        _, final, partials = await run_session(make_provider(server, clock), pcm, clock)
    assert partials
    revisions = [p.revision for p in partials]
    assert revisions == sorted(set(revisions))
    previous_stable, previous_decoded, shrank = '', 0, False
    completed_texts = [s.text for s in final.segments]
    for i, p in enumerate(partials):
        assert p.stable_text.startswith(previous_stable)
        if p.stable_text != previous_stable:
            # stable only grows by whole completed segments
            assert p.stable_text in {''.join(completed_texts[:k]) for k in range(1, len(completed_texts) + 1)}
        assert previous_decoded <= p.decoded_samples <= p.received_samples <= len(pcm) // 2
        if i and len(p.provisional_text) < len(partials[i - 1].provisional_text) and p.stable_text == previous_stable:
            shrank = True
        assert not any(t in p.provisional_text for t in completed_texts if t)
        previous_stable, previous_decoded = p.stable_text, p.decoded_samples
    assert shrank, 'provisional text revision (shorter) must be passed through'


async def test_prov05_final_text_is_ordered_concatenation():
    clock = ScaledClock(20)
    pcm = synth(75, TONE_75)
    async with MockBailianServer(clock) as server:
        _, final, partials = await run_session(make_provider(server, clock), pcm, clock)
    assert final.text == ''.join(s.text for s in final.segments)
    assert final.text.startswith(partials[-1].stable_text)
    assert not final.no_speech
    assert final.processed_samples == len(pcm) // 2


async def test_prov06_all_empty_segments_is_no_speech():
    clock = ScaledClock(20)
    pcm = silence(12)
    async with MockBailianServer(clock, text_fn=lambda audio, a, b: '') as server:
        _, final, _ = await run_session(make_provider(server, clock), pcm, clock)
    assert final.no_speech and final.text == ''


# ---- PROV-07..08 paced catch-up -----------------------------------------------

def rate_profile(appends):
    """Gaps between appends and the worst average send speed (× real time)
    between any two appends 1–2 s apart (audio sent / elapsed)."""
    times = [t for t, _, n in appends]
    sizes = [n for _, _, n in appends]
    gaps = [b - a for a, b in zip(times, times[1:])]
    worst = 0.0
    for i in range(len(times)):
        for j in range(i + 1, len(times)):
            span = times[j] - times[i]
            if span > 2.0:
                break
            if span >= 1.0:
                worst = max(worst, sum(sizes[i:j]) / SAMPLE_RATE / span)
    return gaps, worst


async def test_prov07_catchup_is_paced_at_2x(monkeypatch):
    clock = ScaledClock(4)
    pcm = synth(60)
    sends = []  # provider-side send instants: (clock time, block marker, samples)
    real_append = bailian_conn.append_message

    def recording_append(chunk):
        sends.append((clock.now(), marker_of(chunk), len(chunk) // 2))
        return real_append(chunk)

    monkeypatch.setattr(bailian_conn, 'append_message', recording_append)
    async with MockBailianServer(clock, drop_at=(20.0,), refuse_between=(20.0, 20.4)) as server:
        session, final, partials = await run_session(make_provider(server, clock), pcm, clock)
    # The mock's overspeed rule (receive side) never tripped.
    assert len(server.connections) == 2 and server.overspeed_events == 0
    # Sending interval/speed of the reconnected stream, measured where it is sent.
    second = [s for s in sends if s[0] >= server.connections[1].opened_at]
    gaps, worst = rate_profile(second)
    behind = [g for (t, _, _), g in zip(second, gaps) if t < 40]  # catch-up phase
    assert min(behind) >= 0.125 - 0.010
    assert worst <= 2.05
    # back to real time after catching up: later gaps ≈ 0.25 s
    tail = [g for (t, _, _), g in zip(second, gaps) if t > 50]
    assert tail and min(tail) >= 0.2
    assert final.text == ''.join(expected_text(a, b) for a, b in segment_bounds(final))


async def test_prov08_unpaced_burst_trips_mock_rule(caplog):
    clock = ScaledClock(4)
    pcm = synth(60)
    caplog.set_level(logging.WARNING, logger='svi.asr.bailian')
    async with MockBailianServer(clock, drop_at=(20.0,), refuse_between=(20.0, 20.4)) as server:
        provider = make_provider(server, clock, catchup_rate=float('inf'))  # test-only injection
        session = await provider.open(options())
        with pytest.raises(ProviderError) as info:
            await feed_realtime(session, pcm, clock)
            await session.finish(len(pcm) // 2, loop_deadline(30))
    assert server.overspeed_events > 0
    assert info.value.code == 'bad_request' and not info.value.retryable
    assert any('alert' in r.getMessage() for r in caplog.records)


# ---- PROV-09..11 recovery -----------------------------------------------------

async def test_prov09_resend_from_committed_but_unfinished_segment():
    clock = ScaledClock(60)
    pcm = synth(19 * 35 + 10)  # 20 segments
    async with MockBailianServer(clock, drop_after_commit={8: 0.05}) as server:
        session, final, partials = await run_session(make_provider(server, clock), pcm, clock, deadline_s=60)
    bounds = segment_bounds(final)
    assert len(bounds) == 20
    second = server.connections[1]
    assert second.first_marker * BLOCK == bounds[7][0]
    assert [s.text for s in final.segments] == [expected_text(a, b) for a, b in bounds]
    stables = [p.stable_text for p in partials]
    assert all(b.startswith(a) for a, b in zip(stables, stables[1:]))
    assert session.metrics()['reconnects'] == 1


async def test_prov10_close_1001_twice_recovers():
    clock = ScaledClock(60)
    pcm = synth(1200)
    async with MockBailianServer(clock, close_1001_at=(400.0, 900.0)) as server:
        session, final, _ = await run_session(make_provider(server, clock), pcm, clock, deadline_s=60)
    assert session.metrics()['reconnects'] == 2
    assert [c.close_code for c in server.connections[:2]] == [1001, 1001]
    assert [s.text for s in final.segments] == [expected_text(a, b) for a, b in segment_bounds(final)]
    assert final.segments[-1].end_sample == len(pcm) // 2


@pytest.mark.parametrize('cutoff', [10.0, 40.0])
async def test_prov11_reconnect_limit_then_resumable_failure(cutoff):
    clock = ScaledClock(20)
    pcm = synth(60)
    async with MockBailianServer(clock, refuse_after=cutoff, drop_at=(cutoff,)) as server:
        provider = make_provider(server, clock)
        session = await provider.open(options())
        with pytest.raises(ProviderError) as info:
            await feed_realtime(session, pcm, clock)
            await session.finish(len(pcm) // 2, loop_deadline(30))
    error = info.value
    assert error.code == 'unavailable' and error.retryable
    assert session.reconnects == 8 and server.handshake_attempts == 1 + 8
    completed = [(s.start_sample, s.end_sample, s.text) for s in error.resume.segments]
    if cutoff == 10.0:
        assert completed == []
    else:
        assert completed == [(0, 35 * SAMPLE_RATE, expected_text(0, 35 * SAMPLE_RATE))]


# ---- PROV-12 error mapping ----------------------------------------------------

@pytest.mark.parametrize('case,code,retryable', [
    ('handshake_401', 'auth_failed', False),
    ('handshake_429', 'rate_limited', True),
    ('invalid_request', 'bad_request', False),
])
async def test_prov12_fatal_mappings(case, code, retryable, caplog):
    caplog.set_level(logging.DEBUG)
    clock = ScaledClock(20)
    pcm = synth(40)
    kw = {
        'handshake_401': dict(handshake=lambda n: 401),
        'handshake_429': dict(handshake=lambda n: 429),
        'invalid_request': dict(error_on_commit={1: 'invalid_request_error'}),
    }[case]
    async with MockBailianServer(clock, **kw) as server:
        session = await make_provider(server, clock).open(options())
        with pytest.raises(ProviderError) as info:
            await feed_realtime(session, pcm, clock)
            await session.finish(len(pcm) // 2, loop_deadline(10))
    assert (info.value.code, info.value.retryable) == (code, retryable)
    assert server.handshake_attempts == 1  # no internal reconnect for fatal errors
    assert_private(str(info.value), caplog, server)


async def test_prov12_internal_error_recovers_internally(caplog):
    caplog.set_level(logging.DEBUG)
    clock = ScaledClock(20)
    pcm = synth(75, TONE_75)
    async with MockBailianServer(clock, error_on_commit={1: 'InternalError'}) as server:
        session, final, _ = await run_session(make_provider(server, clock), pcm, clock)
    assert session.metrics()['reconnects'] == 1 and len(final.segments) == 3
    assert_private('', caplog, server)


async def test_prov12_finish_timeout_is_retryable(caplog):
    caplog.set_level(logging.DEBUG)
    clock = ScaledClock(20)
    pcm = synth(12)
    async with MockBailianServer(clock, never_complete=True) as server:
        session = await make_provider(server, clock).open(options())
        await feed_realtime(session, pcm, clock)
        with pytest.raises(ProviderError) as info:
            await session.finish(len(pcm) // 2, loop_deadline(0.5))
    assert (info.value.code, info.value.retryable) == ('timeout', True)
    assert_private(str(info.value), caplog, server)


def assert_private(error_text, caplog, server):
    # Every product-side record (svi.*, websockets client, asyncio); only the mock
    # cloud's own server logs are excluded.
    text = error_text + '\n'.join(r.getMessage() for r in caplog.records if not r.name.startswith(MOCK_LOGGER))
    for secret in (TEST_KEY, server.url, 'Error committing', 'Internal server error', 'seg['):
        assert secret not in text


# ---- PROV-13..16 lifecycle, hints, concurrency, proxy -------------------------

async def test_prov13_cancel_is_idempotent_and_releases():
    clock = ScaledClock(10)
    pcm = synth(40)
    async with MockBailianServer(clock) as server:
        provider = make_provider(server, clock, max_concurrent_sessions=1)
        session = await provider.open(options())
        await feed_realtime(session, pcm, clock, until_s=36)  # one segment completed
        await asyncio.sleep(0.1)
        assert session._stable_text()
        started = asyncio.get_running_loop().time()
        await session.cancel()
        await session.cancel()
        assert asyncio.get_running_loop().time() - started <= 2.0
        await asyncio.sleep(0.05)
        assert server.connections[0].closed_at is not None
        assert not session.pcm and session._stable_text() == '' and all(s.text is None for s in session.segments)
        again = await provider.open(options(session_id='sess-v2-next'))
        await again.cancel()


async def test_prov14_no_hint_terms_sent():
    clock = ScaledClock(20)
    pcm = synth(5)
    async with MockBailianServer(clock) as server:
        provider = make_provider(server, clock)
        await run_session(provider, pcm, clock, hint_terms=['React', 'Docker'])
    update = server.connections[0].session_updates[0]
    payload = json.dumps(update)
    assert 'corpus' not in payload and 'React' not in payload and 'Docker' not in payload
    assert update['session']['turn_detection'] is None
    assert provider.capabilities.supports_hints is False


async def test_prov15_provider_concurrency_limit():
    clock = ScaledClock(10)
    async with MockBailianServer(clock) as server:
        provider = make_provider(server, clock, max_concurrent_sessions=2)
        a = await provider.open(options(session_id='a'))
        b = await provider.open(options(session_id='b'))
        with pytest.raises(ProviderError) as info:
            await provider.open(options(session_id='c'))
        assert (info.value.code, info.value.retryable) == ('busy', True)
        await a.cancel()
        c = await provider.open(options(session_id='c'))
        await asyncio.gather(b.cancel(), c.cancel())


@pytest.mark.parametrize('use_env_proxy', [False, True])
async def test_prov16_environment_proxy_ignored(monkeypatch, use_env_proxy):
    hits = []

    async def proxy(reader, writer):
        hits.append(1)
        writer.close()

    fake = await asyncio.start_server(proxy, '127.0.0.1', 0)
    address = f"http://127.0.0.1:{fake.sockets[0].getsockname()[1]}"
    for name in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
        monkeypatch.setenv(name, address)
    for name in ('NO_PROXY', 'no_proxy'):
        monkeypatch.setenv(name, '')
    clock = ScaledClock(20)
    pcm = synth(5)
    try:
        async with MockBailianServer(clock) as server:
            provider = make_provider(server, clock, use_env_proxy=use_env_proxy, max_reconnects=0)
            if use_env_proxy:  # control: the fake proxy is really reachable from env
                session = await provider.open(options())
                with pytest.raises(ProviderError):
                    await feed_realtime(session, pcm, clock)
                    await session.finish(len(pcm) // 2, loop_deadline(5))
                assert hits
            else:
                _, final, _ = await run_session(provider, pcm, clock)
                assert final.text and hits == []
    finally:
        fake.close()


# ---- PROV-17 pinned validated address -----------------------------------------

async def test_prov17_connect_uses_validated_ip(monkeypatch):
    clock = ScaledClock(20)
    pcm = synth(5)
    answers = [['8.8.8.8']]
    lookups = []

    async def resolver(host, port):
        lookups.append(host)
        result = answers[0]
        answers[0] = ['10.0.0.5']  # DNS changes right after validation
        return result

    egress = EgressPolicy('allowlist', ['*.maas.aliyuncs.com'], resolver=resolver)
    seen = []
    async with MockBailianServer(clock) as server:
        mock_port = int(server.url.rsplit(':', 1)[1])

        async def fake_open(uri, **kwargs):
            seen.append(dict(uri=uri, host=kwargs.get('host'), server_hostname=kwargs.get('server_hostname'),
                             ssl=kwargs.get('ssl') is not None))
            kwargs = {k: v for k, v in kwargs.items() if k not in ('ssl', 'server_hostname', 'host', 'port')}
            return await websockets.connect(f'ws://127.0.0.1:{mock_port}/api-ws/v1/realtime', **kwargs)

        monkeypatch.setattr(bailian_conn, '_open_ws', fake_open)
        config = BailianRealtimeConfig(base_url='wss://ws-abc123.cn-beijing.maas.aliyuncs.com', api_key=TEST_KEY)
        provider = BailianRealtimeProvider(config, egress=egress, clock=clock)
        _, final, _ = await run_session(provider, pcm, clock)
    assert final.text
    assert lookups == ['ws-abc123.cn-beijing.maas.aliyuncs.com']
    assert seen[0]['host'] == '8.8.8.8' and seen[0]['ssl']
    assert seen[0]['server_hostname'] == 'ws-abc123.cn-beijing.maas.aliyuncs.com'
    assert seen[0]['uri'].startswith('wss://ws-abc123.cn-beijing.maas.aliyuncs.com/')


async def test_prov17_private_resolution_denied_before_connect(monkeypatch):
    async def resolver(host, port):
        return ['10.0.0.5']

    opened = []

    async def fake_open(uri, **kwargs):
        opened.append(uri)
        raise AssertionError('must not connect')

    monkeypatch.setattr(bailian_conn, '_open_ws', fake_open)
    clock = ScaledClock(20)
    config = BailianRealtimeConfig(base_url='wss://ws-abc123.cn-beijing.maas.aliyuncs.com', api_key=TEST_KEY)
    provider = BailianRealtimeProvider(
        config, egress=EgressPolicy('allowlist', ['*.maas.aliyuncs.com'], resolver=resolver), clock=clock)
    session = await provider.open(options())
    await asyncio.sleep(0.2)
    await session.feed(b'\0\0' * BLOCK, 0)  # upload continues; failure is reported by finish
    with pytest.raises(ProviderError) as info:
        await session.finish(BLOCK, loop_deadline(2))
    assert info.value.code == 'egress_denied' and not info.value.retryable
    assert opened == []
    await session.cancel()


# ---- PROV-18 log privacy over the recovery/pacing/error scenarios --------------

async def test_prov18_logs_contain_only_metadata(caplog):
    caplog.set_level(logging.DEBUG)
    clock = ScaledClock(20)
    pcm = synth(75, TONE_75)
    async with MockBailianServer(clock, drop_after_commit={1: 0.05}, error_on_commit={2: 'InternalError'}) as server:
        session, final, _ = await run_session(
            make_provider(server, clock), pcm, clock)
    assert session.metrics()['reconnects'] == 2
    records = [r.getMessage() for r in caplog.records if r.name.startswith('svi.asr')]
    assert records, 'recovery must be logged'
    joined = '\n'.join(records)
    assert TEST_KEY not in joined and server.url not in joined and 'seg[' not in joined
    assert 'ws-' not in joined.replace('ws-…', '')
    assert 'segment=' in joined and 'session=' in joined


# ---- Real-cloud finding 2026-10-28: pure silence transcribed as "嗯。" ---------

@pytest.mark.parametrize('pcm_factory,expect_empty', [(lambda: silence(12), True), (lambda: synth(5), False)])
async def test_silence_filler_dropped_only_for_unvoiced_segments(pcm_factory, expect_empty):
    clock = ScaledClock(20)
    pcm = pcm_factory()
    async with MockBailianServer(clock, text_fn=lambda audio, a, b: '嗯。') as server:
        _, final, _ = await run_session(make_provider(server, clock), pcm, clock)
    if expect_empty:
        assert final.no_speech and final.text == ''
    else:  # a voiced segment keeps a real filler word verbatim
        assert final.text == '嗯。' and not final.no_speech
