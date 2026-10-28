"""Real PG/two production ASGI WS; synthetic PCM and Mock model providers.

Queue another account's PCM exactly when production calls the synchronous
selector. No clocks/deadlines/WS routes replaced. This tests selector contention,
not physical audio, N20 throughput, or TCP network buffers.
"""
import asyncio
import json
import struct
import uuid

import pytest

from sqlalchemy import insert, select, update

from app.models import DictionaryState, DictionaryTerm, User
from app.services import session_runtime
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_normal_shutdown_contract import WsProbe


@pytest.mark.parametrize('dictionary_kind',['ascii','chinese-long'])
def test_full_dictionary_selection_preserves_other_accounts_two_second_ack(
    app_client, monkeypatch, record_property, dictionary_kind,
):
    target_headers = auth_headers(register_and_login(app_client, email='dictionary-target@example.invalid'))
    peer_headers = auth_headers(register_and_login(app_client, email='dictionary-peer@example.invalid'))
    app = app_client.app
    assert app.state.config.asr.provider == app.state.config.llm.provider == 'mock'
    observations = {'dictionary_kind':dictionary_kind}
    actual = session_runtime.refine_hints

    async def run():
        loop = asyncio.get_running_loop()
        async with app.state.db.session_factory() as db:
            owner = await db.scalar(select(User.id).where(User.email == 'dictionary-target@example.invalid'))
            await db.execute(insert(DictionaryTerm), [dict(id=str(uuid.uuid4()), user_id=owner,
                term=(f'ProjectTerm{i:04d}' if dictionary_kind=='ascii' else '项目术语'+'X'*56+f'{i:04d}'),
                norm=(f'projectterm{i:04d}' if dictionary_kind=='ascii' else '项目术语'+'x'*56+f'{i:04d}'),
                aliases=[(f'ProjectAlias{i:04d}x{a:02d}' if dictionary_kind=='ascii' else '项目别名'+'Y'*54+f'{i:04d}{a:02d}') for a in range(10)], contexts=[])
                for i in range(5000)])
            await db.execute(update(DictionaryState).where(DictionaryState.user_id == owner).values(version=1))
            await db.commit()
        wires = []
        try:
            for headers in (peer_headers, target_headers):
                rid = str(uuid.uuid4())
                # Use actual client from another thread through the portal would
                # deadlock; ASGI HTTP stays in this application's current loop.
                import httpx
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as http:
                    response = await http.post('/api/v1/sessions', headers=headers | {'Idempotency-Key':rid},
                        json={'client_request_id':rid,'mode':'dictation','learning_enabled':False})
                assert response.status_code == 201
                wire = WsProbe(app, headers, response.json()['session_id'])
                wires.append(wire)
                await wire.start()
            peer, target = wires
            runtime = app.state.sessions.runtimes[target.session_id]
            assert len(runtime.dictionary_snapshot) == 5000
            assert sum(len(e['aliases']) for e in runtime.dictionary_snapshot) == 50000
            assert not app.state.sessions.runtimes[peer.session_id].dictionary_snapshot
            triggered = False
            def observed(snapshot, raw):
                nonlocal triggered
                assert len(snapshot) == 5000 and not triggered
                triggered = True
                observations['peer_pcm_queued_at'] = loop.time()
                peer.incoming.put_nowait({'type':'websocket.receive',
                    'bytes':struct.pack('<II',1,4000)+struct.pack('<h',100)*4000})
                started = loop.time()
                result = actual(snapshot, raw)
                observations['selector_ms'] = (loop.time()-started)*1000
                observations['selected_count'] = len(result)
                return result
            monkeypatch.setattr(session_runtime,'refine_hints',observed)
            async def read_ack():
                # Observe a late ACK without accepting it: the product assertion
                # below remains the original strict 2000 ms boundary.
                async with asyncio.timeout(15):
                    return await peer.outgoing.get()
            ack_task = asyncio.create_task(read_ack())
            await target.end()
            ack_message = await ack_task
            assert ack_message['type'] == 'websocket.send'
            ack = json.loads(ack_message['text'])
            observations['peer_ack_ms'] = (loop.time()-observations['peer_pcm_queued_at'])*1000
            assert ack['type']=='audio.ack' and ack['seq']==1 and ack['total_samples']==8000
            await target.drain()
            assert target.close_code == 1000
            assert any(e['type']=='result.ready' for e in target.events)
        finally:
            for wire in wires:
                if wire.task and not wire.task.done():
                    await wire.send({'type':'websocket.disconnect','code':1000})
                    async with asyncio.timeout(10): await wire.task
        assert all(w.task.done() and w.task.exception() is None for w in wires)
    try:
        app_client.portal.call(run)
    finally:
        record_property('dictionary_selection_observation',json.dumps(observations,sort_keys=True))
    assert observations['peer_ack_ms'] < 2000, observations
