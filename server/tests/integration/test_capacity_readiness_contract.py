"""Persisted capacity above the deployment ceiling has one documented reason."""
import pytest
from fastapi.testclient import TestClient

from app.core.config import Config, SESSION_PCM_RESERVE, load_config
from tests.conftest import (SERVER_ROOT, TEST_URL, auth_headers, register_and_login,
                            wait_for_readiness, wait_startup_completion)
from tests.cases.test_dict_cases import fetch_one
from tests.ids import fixture_uuid
from tests.test_admin import login_admin
from tests.test_sessions import create_session


@pytest.mark.parametrize('ceiling_source', ['deployment', 'pcm'])
def test_saved_capacity_above_smaller_deployment_has_contract_reason_and_recovers(clean_database, monkeypatch, ceiling_source):
    monkeypatch.setenv('SVI_DATABASE_URL', TEST_URL)
    monkeypatch.setenv('SVI_CONFIG', str(SERVER_ROOT / 'config/app.yaml'))
    monkeypatch.setenv('SVI_INSTANCE_LOCK', '0')
    monkeypatch.setenv('SVI_ENVIRONMENT', 'development')
    monkeypatch.setenv('SVI_ASR_PROVIDER', 'mock')
    monkeypatch.setenv('SVI_LLM_PROVIDER', 'mock')
    from app import main as main_module

    base = load_config()
    payload = base.model_dump()
    payload['sessions'].update(initial_max_active=10,
        admission_ceiling=10 if ceiling_source == 'deployment' else 20)
    if ceiling_source == 'pcm':
        payload['audio']['active_pcm_budget_bytes'] = 10 * SESSION_PCM_RESERVE
    validated = Config.model_validate(payload)
    smaller = base.model_copy(update={'sessions': validated.sessions, 'audio': validated.audio})
    assert smaller.effective_admission_ceiling == 10
    current = {'config': base}
    monkeypatch.setattr(main_module, 'load_config', lambda *args, **kwargs: current['config'])
    with TestClient(main_module.create_app()) as first:
        wait_for_readiness(first)
        assert first.portal.call(first.app.state.capacity.current) == 20
        administrator = auth_headers(login_admin(first))
        member = auth_headers(register_and_login(first, email='capacity09-member@example.com'))
    current['config'] = smaller
    with TestClient(main_module.create_app()) as restarted:
        wait_startup_completion(restarted)
        rejected = restarted.get('/health/ready')
        assert rejected.status_code == 503
        assert rejected.json()['reason_code'] == 'CAPACITY_CONFIG_INVALID'
        assert restarted.get('/healthz').json()['unavailable_reason'] == 'CAPACITY_CONFIG_INVALID'
        # The stored value was not silently reduced on startup.
        assert restarted.portal.call(restarted.app.state.capacity.current) == 20

        # An unchanged valid desktop account can still authenticate while
        # only new dictation/retry admission is closed.
        assert restarted.get('/api/users/me', headers=member).status_code == 200
        login = restarted.post('/api/auth/login', json={
            'email': 'capacity09-member@example.com', 'password': 'SmokePass123',
            'client_type': 'desktop', 'device': {
                'installation_id': fixture_uuid('test-installation'), 'name': 'test-mac',
                'platform': 'macos', 'client_version': '0.1.0',
            },
        })
        assert login.status_code == 200
        member = auth_headers(login.json())
        before = tuple(fetch_one(f'SELECT count(*) FROM {table}')
                       for table in ('sessions', 'session_attempts', 'operation_requests'))
        denied = create_session(restarted, member, 'capacity09-admission')
        assert denied.status_code == 503
        assert denied.json()['error']['code'] == 'SERVICE_UNAVAILABLE'
        assert tuple(fetch_one(f'SELECT count(*) FROM {table}')
                     for table in ('sessions', 'session_attempts', 'operation_requests')) == before
        assert restarted.app.state.sessions.runtimes == {}

        manageable = restarted.get('/api/admin/capacity', headers=administrator)
        assert manageable.status_code == 200
        assert manageable.json()['config']['max_active_sessions'] == 20
        assert manageable.json()['deployment']['effective_ceiling'] == 10
        assert manageable.json()['deployment']['admission_ceiling'] == (
            10 if ceiling_source == 'deployment' else 20)
        if ceiling_source == 'pcm':
            assert manageable.json()['deployment']['active_pcm_budget_bytes'] == 10 * SESSION_PCM_RESERVE
        repaired = restarted.patch('/api/admin/capacity', headers=administrator,
            json={'version': manageable.json()['config']['version'], 'max_active_sessions': 10})
        assert repaired.status_code == 200
        assert repaired.json()['config']['max_active_sessions'] == 10
        wait_for_readiness(restarted, pending_reason='CAPACITY_CONFIG_INVALID')
        assert restarted.get('/health/ready').status_code == 200
        assert restarted.portal.call(restarted.app.state.capacity.current) == 10
        # The rejected request did not consume its identity/key or reserve a slot.
        accepted = create_session(restarted, member, 'capacity09-admission')
        assert accepted.status_code == 201 and accepted.json()['replayed'] is False
        session_id = accepted.json()['session_id']
        assert fetch_one('SELECT count(*) FROM sessions') == before[0] + 1
        assert fetch_one('SELECT count(*) FROM session_attempts') == before[1] + 1
        cancelled = restarted.post(f'/api/v1/sessions/{session_id}/cancel', headers=member)
        assert cancelled.status_code == 200
