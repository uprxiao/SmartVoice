"""Unknown current-attempt metadata must not become a guessed PCM metric."""
from sqlalchemy import select

from app.models import VoiceSession
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_window_metrics import metrics_client
from tests.test_admin import login_admin
from tests.test_sessions import create_session


def test_missing_current_attempt_makes_runtime_unknown_then_recovers(metrics_client):
    client = metrics_client
    admin = auth_headers(login_admin(client))
    headers = auth_headers(register_and_login(client, email='unknown-metric@example.invalid'))
    response = create_session(client, headers, 'unknown-current-attempt')
    assert response.status_code == 201
    sid = response.json()['session_id']

    async def set_current(number):
        async with client.app.state.db.session_factory() as db:
            session = await db.scalar(select(VoiceSession).where(VoiceSession.id == sid))
            assert session.state == 'created'
            session.current_attempt_no = number
            await db.commit()

    original = client.get('/api/admin/capacity', headers=admin).json()
    assert original['runtime']['reserved_pcm_bytes'] == 5760000
    try:
        # Controlled corruption in this process-owned test DB only; no state,
        # audio, identity or report replacement. No current attempt row exists.
        client.portal.call(set_current, 2)
        unknown = client.get('/api/admin/capacity', headers=admin)
        assert unknown.status_code == 200
        assert unknown.json()['runtime'] is None
        assert unknown.json()['config'] == original['config']
        assert unknown.json()['validation'] == original['validation']
    finally:
        client.portal.call(set_current, 1)
        recovered = client.get('/api/admin/capacity', headers=admin).json()
        assert recovered['runtime']['reserved_pcm_bytes'] == 5760000
        assert recovered['runtime']['active_sessions'] == 1
        assert client.post(f'/api/v1/sessions/{sid}/cancel', headers=headers).status_code == 200
