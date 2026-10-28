"""API05/API11 controlled timing, real ASGI WebSocket and PostgreSQL.

Copy this frozen file to a new server/tests/integration path only after the
earlier source-frozen batch finishes. No production state or deadline is set.
Physical ASR/LLM methods are gated existing Mocks, not real model performance.
"""
from __future__ import annotations

import asyncio
import json
import struct

import httpx
import pytest

from tests.conftest import auth_headers, register_and_login
from tests.integration.test_normal_shutdown_contract import WsProbe
from tests.integration.test_voice_cancel_boundaries import create, facts, settle


@pytest.mark.parametrize('mode', [
    'api05-asr-pending', 'api05-refine-pending',
    'api11-identical-three-ends', 'api11-changed-after-identical',
])
def test_end_protocol_before_ready_has_deterministic_terminal_and_single_finalization(
    app_client, monkeypatch, record_property, mode,
):
    pair = register_and_login(app_client, email='r2-end-protocol@example.invalid')
    headers, owner = auth_headers(pair), pair['user']['id']
    stage = 'refine' if mode == 'api05-refine-pending' else 'asr'
    success = mode == 'api11-identical-three-ends'

    async def scenario():
        app, manager = app_client.app, app_client.app.state.sessions
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        trace = dict(process_starts=0, finish_entries=0, finish_returns=0,
                     refine_entries=0, refine_returns=0, gate_cancels=0)
        wire = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://testserver') as http:
            sid = await create(http, headers, 'r2-end-protocol-'+mode)
            runtime, llm = manager.runtimes[sid], app.state.llm
            stream, original_audio = runtime.asr, runtime.audio
            actual_finish, actual_generate = stream.finish, llm.inner.generate
            actual_start = manager._start_processing
            assert app.state.config.asr.finish_timeout_ms == 10000
            assert app.state.config.llm.call_timeout_ms == 10000
            assert app.state.config.pipeline.stop_to_result_budget_ms == 30000

            async def gate(deadline):
                assert 0 < deadline-asyncio.get_running_loop().time() <= 10
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    trace['gate_cancels'] += 1
                    cancelled.set()
                    raise
                assert asyncio.get_running_loop().time() < deadline

            async def finish(total_samples, deadline):
                assert total_samples == stream.samples == 4000
                trace['finish_entries'] += 1
                if stage == 'asr':
                    await gate(deadline)
                answer = await actual_finish(total_samples, deadline)
                trace['finish_returns'] += 1
                return answer

            async def generate(request, deadline):
                assert request.purpose == 'refine' and request.output_mode == 'text'
                trace['refine_entries'] += 1
                if stage == 'refine':
                    await gate(deadline)
                answer = await actual_generate(request, deadline)
                trace['refine_returns'] += 1
                return answer

            def start(current, coroutine):
                if current is runtime:
                    trace['process_starts'] += 1
                return actual_start(current, coroutine)

            monkeypatch.setattr(stream, 'finish', finish)
            monkeypatch.setattr(llm.inner, 'generate', generate)
            monkeypatch.setattr(manager, '_start_processing', start)
            try:
                wire = WsProbe(app, headers, sid)
                await wire.start()
                assert len(original_audio) == 8000 and runtime.samples == 4000
                await wire.end()
                async with asyncio.timeout(3):
                    await entered.wait()
                before = await facts(app, owner, sid)
                assert before['session']['state'] == ('refining' if stage == 'refine' else 'finalizing')
                assert before['session']['audio_complete'] is True
                assert before['counts']['results'] == 0
                assert trace['process_starts'] == trace['finish_entries'] == 1
                assert not runtime.task.done()
                original_task = runtime.task
                original_end, original_deadline = runtime.end_accepted_at, runtime.deadline
                assert original_deadline == original_end + 30

                if mode.startswith('api11-'):
                    for _ in range(2):
                        await wire.send({'type': 'websocket.receive', 'text': json.dumps({
                            'type': 'audio.end', 'last_seq': 0, 'total_samples': 4000})})
                        ack = await wire.event()
                        assert ack['type'] == 'audio.end_ack'
                        assert (ack['last_seq'], ack['total_samples'], ack['attempt_no']) == (0, 4000, 1)
                        assert runtime.task is original_task
                        assert runtime.end_accepted_at == original_end and runtime.deadline == original_deadline
                        assert trace['process_starts'] == trace['finish_entries'] == 1
                    if not success:
                        await wire.send({'type': 'websocket.receive', 'text': json.dumps({
                            'type': 'audio.end', 'last_seq': 0, 'total_samples': 8000})})
                else:
                    # Actual new data after durable end, while the gate proves
                    # ready has not committed. No sleep-based racing oracle.
                    await wire.send({'type': 'websocket.receive',
                        'bytes': struct.pack('<II', 1, 4000)+struct.pack('<h', 123)*4000})
                wire.drain_task = asyncio.create_task(wire.drain())
                if success:
                    release.set()
                await wire.drain_task
                after = await facts(app, owner, sid)
                response = await http.get(f'/api/v1/sessions/{sid}', headers=headers)
                assert response.status_code == 200
                detail = response.json()
                assert detail['received_samples'] == after['session']['received_samples'] == 4000
                assert detail['audio_complete'] is True
                assert runtime.audio == original_audio == b'' and runtime.asr is None
                assert not runtime.hint_terms and not runtime.refine_terms and not runtime.dictionary_snapshot
                assert trace['process_starts'] == trace['finish_entries'] == 1
                assert runtime.task is None or runtime.task.done()
                assert after['counts']['insertions'] == after['counts']['learning_events'] == 0
                types = [event['type'] for event in wire.events]
                assert types.count('audio.end_ack') == (3 if mode.startswith('api11-') else 1)
                assert not any(event['type'] == 'audio.ack' and event['seq'] == 1 for event in wire.events)
                if success:
                    assert wire.close_code == 1000
                    assert types.count('asr.final') == types.count('result.ready') == 1
                    assert types.count('error') == 0
                    assert after['session']['state'] == after['attempt']['state'] == detail['state'] == 'ready'
                    assert after['counts']['results'] == after['counts']['model_calls'] == 1
                    assert detail['current_result_id'] == detail['result']['result_id']
                    assert detail['result']['review_required'] is False
                    assert trace['finish_returns'] == trace['refine_entries'] == trace['refine_returns'] == 1
                    assert trace['gate_cancels'] == 0
                else:
                    async with asyncio.timeout(3):
                        await cancelled.wait()
                    assert wire.close_code == 4400
                    assert [event['error']['code'] for event in wire.events if event['type'] == 'error'] == ['AUDIO_PROTOCOL_ERROR']
                    assert types.count('result.ready') == 0
                    assert types.count('asr.final') == (1 if stage == 'refine' else 0)
                    assert after['session']['state'] == after['attempt']['state'] == detail['state'] == 'failed'
                    assert after['session']['error_code'] == 'AUDIO_PROTOCOL_ERROR'
                    assert detail['result'] is None and detail['current_result_id'] is None
                    assert after['counts']['results'] == 0 and not after['attempt']['has_raw']
                    assert after['counts']['model_calls'] == (1 if stage == 'refine' else 0)
                    assert trace['finish_returns'] == (1 if stage == 'refine' else 0)
                    assert trace['refine_entries'] == (1 if stage == 'refine' else 0)
                    assert trace['refine_returns'] == 0 and trace['gate_cancels'] >= 1
                # Independent read after the completed protocol handler: no
                # late result, second attempt, or duplicated permanent usage.
                assert await facts(app, owner, sid) == after
                return dict(mode=mode, gate=stage, close_code=wire.close_code,
                    terminal=detail['state'], original_deadline_preserved=runtime.deadline == original_deadline,
                    received_samples=detail['received_samples'], counts=after['counts'],
                    end_ack_count=types.count('audio.end_ack'), final_count=types.count('result.ready'),
                    trace=trace, physical_models=False, native_insertions=False)
            finally:
                release.set()
                await settle(wire)

    record_property('r2_end_protocol', json.dumps(app_client.portal.call(scenario), sort_keys=True))
