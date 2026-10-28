"""Fixed persistence contract only: Mock providers, synthetic PCM, no model tuning."""
import hashlib
import json

from sqlalchemy import text

from tests.conftest import auth_headers, register_and_login
from tests.test_sessions import create_session, dictation


def test_next_dictation_and_cancel_keep_each_completed_result_readable(app_client, monkeypatch, record_property):
    client = app_client
    headers = auth_headers(register_and_login(client))
    expected = {}

    def history():
        response = client.get('/api/v1/history', headers=headers)
        assert response.status_code == 200
        return {item['session_id']: item for item in response.json()['items']}

    for index, fixed in enumerate(('第一段虚构记录：保留 API。', '第二段虚构记录：保留 Docker。')):
        # The fixture supplies fixed text; this does not measure ASR/LLM quality.
        monkeypatch.setattr(client.app.state.asr, 'text', fixed)
        response = create_session(client, headers, f'consecutive-history-{index}')
        assert response.status_code == 201
        sid = response.json()['session_id']
        # Starting another session must not consume, clear or reassign older results.
        during = history()
        for old_sid, saved in expected.items():
            assert during[old_sid]['result'] == saved
            assert during[old_sid]['state'] == 'ready'
        events = dictation(client, headers, sid)
        assert events[-1]['type'] == 'result.ready'
        result = events[-1]['result']
        assert result['raw_text'] == fixed
        expected[sid] = result

    interrupted = create_session(client, headers, 'consecutive-history-cancel')
    assert interrupted.status_code == 201
    third_sid = interrupted.json()['session_id']
    cancelled = client.post(f'/api/v1/sessions/{third_sid}/cancel', headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()['state'] == 'cancelled'
    final = history()
    assert set(final) == {*expected, third_sid}
    assert final[third_sid]['result'] is None
    for sid, saved in expected.items():
        detail = client.get(f'/api/v1/sessions/{sid}', headers=headers)
        assert detail.status_code == 200
        assert detail.json()['result'] == final[sid]['result'] == saved

    async def independent_readback():
        async with client.app.state.db.session_factory() as connection:
            await connection.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            saved = (await connection.execute(text('SELECT session_id,raw_text,refined_text FROM results'))).all()
            counts = (await connection.execute(text('SELECT (SELECT count(*) FROM sessions), (SELECT count(*) FROM insertions)'))).one()
            return saved, counts

    saved, counts = client.portal.call(independent_readback)
    assert tuple(counts) == (3, 0)
    assert {sid: (raw, refined) for sid, raw, refined in saved} == {
        sid: (result['raw_text'], result['refined_text']) for sid, result in expected.items()
    }
    record_property('consecutive_history', json.dumps({
        'scope': 'formal HTTP/ASGI WS and PostgreSQL; Mock ASR/LLM; no Desktop, microphone, native or clipboard claim',
        'completed_results': len(saved), 'cancelled_without_result': 1, 'insertions': 0,
        'original_text_hashes': [hashlib.sha256(result['raw_text'].encode()).hexdigest() for result in expected.values()],
        'older_results_unchanged_while_next_active': True,
        'detail_and_history_match_durable_results': True,
    }, sort_keys=True))
