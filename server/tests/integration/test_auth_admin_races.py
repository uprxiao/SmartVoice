"""Real PostgreSQL integration drafts; never run against a production database."""
from concurrent.futures import ThreadPoolExecutor
import uuid
import time

from tests.conftest import execute_sql


def account(client, email, *, admin=False):
    response = client.post('/api/auth/register', json={'email': email, 'password': 'SmokePass123'})
    assert response.status_code == 201, response.text
    user = response.json()
    if admin:
        execute_sql("UPDATE users SET role='admin' WHERE id=$1", user['id'])
    body = {'email': email, 'password': 'SmokePass123', 'client_type': 'admin_web' if admin else 'desktop'}
    if not admin:
        body['device'] = dict(installation_id=str(uuid.uuid4()), name='test', platform='macos', client_version='0.0.1')
    response = client.post('/api/auth/login', json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def headers(token):
    return {'Authorization': 'Bearer ' + token['access_token']}


def test_concurrent_duplicate_registration_has_one_201_and_one_409(app_client):
    def create(_):
        return app_client.post('/api/auth/register', json={'email': 'race@example.com', 'password': 'SmokePass123'})
    with ThreadPoolExecutor(max_workers=2) as executor:
        replies = list(executor.map(create, range(2)))
    assert sorted(reply.status_code for reply in replies) == [201, 409]
    assert next(reply for reply in replies if reply.status_code == 409).json()['error']['code'] == 'EMAIL_EXISTS'


def test_disabled_tokens_and_refresh_return_403_not_401(app_client):
    admin, _ = account(app_client, 'admin@example.com', admin=True)
    user, _ = account(app_client, 'user@example.com')
    response = app_client.patch('/api/admin/users/' + user['user']['id'], json={'status': 'disabled'}, headers=headers(admin))
    assert response.status_code == 200, response.text
    for response in (app_client.get('/api/users/me', headers=headers(user)),
                     app_client.post('/api/auth/refresh', json={'refresh_token': user['refresh_token']})):
        assert response.status_code == 403, response.text
        assert response.json()['error']['code'] == 'ACCOUNT_DISABLED'
    assert app_client.patch('/api/admin/users/' + user['user']['id'], json={'status': 'active'}, headers=headers(admin)).status_code == 200
    # Re-enabling does not resurrect a revoked sid.
    assert app_client.get('/api/users/me', headers=headers(user)).status_code == 401


def test_admin_password_is_part_of_idempotency_payload(app_client):
    admin, _ = account(app_client, 'admin@example.com', admin=True)
    h = headers(admin) | {'Idempotency-Key': str(uuid.uuid4())}
    body = {'email': 'created@example.com', 'password': 'SmokePass123'}
    first = app_client.post('/api/admin/users', json=body, headers=h)
    assert first.status_code == 201, first.text
    replay = app_client.post('/api/admin/users', json=body, headers=h)
    assert replay.status_code == 200 and replay.json()['replayed'] is True
    changed = app_client.post('/api/admin/users', json=body | {'password': 'Different123'}, headers=h)
    assert changed.status_code == 409 and changed.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'


def test_concurrent_admin_disables_do_not_remove_all_admins(app_client):
    first, _ = account(app_client, 'first@example.com', admin=True)
    second, _ = account(app_client, 'second@example.com', admin=True)
    def disable(pair):
        actor, target = pair
        return app_client.patch('/api/admin/users/' + target['user']['id'], json={'status': 'disabled'}, headers=headers(actor))
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(disable, [(first, second), (second, first)]))
    assert sum(response.status_code == 200 for response in responses) == 1
    assert all(response.status_code in (200, 403) for response in responses)
    states = [app_client.get('/api/users/me', headers=headers(token)).status_code for token in (first, second)]
    assert sorted(states) == [200, 403]


def test_readiness_recovers_after_persisted_capacity_is_lowered(app_client):
    admin, _ = account(app_client, 'admin@example.com', admin=True)
    ceiling = app_client.app.state.config.effective_admission_ceiling
    execute_sql('UPDATE runtime_capacity SET max_active_sessions=$1 WHERE id=1', ceiling + 1)
    def wait_status(expected):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            response = app_client.get('/health/ready')
            if response.status_code == expected:
                return
            time.sleep(.05)
        raise AssertionError(response.text)
    wait_status(503)
    response = app_client.get('/api/admin/capacity', headers=headers(admin))
    assert response.status_code == 200, response.text
    current = response.json()['config']
    fixed = app_client.patch('/api/admin/capacity', json={'version': current['version'], 'max_active_sessions': ceiling}, headers=headers(admin))
    assert fixed.status_code == 200, fixed.text
    wait_status(200)
    assert app_client.get('/health/live').status_code == 200


def test_cors_preflight_accepts_idempotency_key(app_client):
    origin = app_client.app.state.config.security.cors_origins[0]
    response = app_client.options('/api/admin/users', headers={'Origin': origin,
        'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'Idempotency-Key,Content-Type,Authorization'})
    assert response.status_code == 200, response.text
    assert 'idempotency-key' in response.headers['access-control-allow-headers'].lower()
