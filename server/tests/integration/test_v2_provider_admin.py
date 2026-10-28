"""TC-V2-CFG / PAPI / LLM (environment I): admin provider API on real FastAPI +
PostgreSQL, cloud ASR = mock Bailian service, LLM = in-process mock types.
Fictional keys only (``sk-test-…``); master keys are random per test.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import subprocess
import uuid

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.egress import EgressPolicy
from app.core.secrets import SecretBox, SecretError, associated_data
from app.providers.asr import bailian_conn
from app.providers.llm.mock import MockLlmProvider
from tests.bailian_audio import ScaledClock, synth
from tests.conftest import SERVER_ROOT, TEST_URL, auth_headers, execute_sql, register_and_login, wait_for_readiness
from tests.integration.test_v2_cloud_runtime import ThreadedMock, sql, stream_audio, v2_yaml, wait_terminal

KEY = 'sk-test-' + secrets.token_hex(12)
KEY2 = 'sk-test-' + secrets.token_hex(12)


def master_keys():
    return {'k1': base64.b64encode(secrets.token_bytes(32)).decode(),
            'k2': base64.b64encode(secrets.token_bytes(32)).decode()}


@pytest.fixture()
def keys():
    return master_keys()


def start_app(monkeypatch, tmp_path, keys, *, security=None, environment='development', extra=None,
              instance_lock=False):
    path = v2_yaml(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['environment'] = environment
    if security:
        data.setdefault('security', {}).update(security)
    for section, values in (extra or {}).items():
        data.setdefault(section, {}).update(values)
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    monkeypatch.setenv('SVI_DATABASE_URL', TEST_URL)
    monkeypatch.setenv('SVI_INSTANCE_LOCK', '1' if instance_lock else '0')
    monkeypatch.setenv('SVI_ENVIRONMENT', environment)
    if keys is None:
        monkeypatch.delenv('SVI_SECRET_KEYS', raising=False)
        monkeypatch.delenv('SVI_SECRET_ACTIVE_KEY_ID', raising=False)
    else:
        monkeypatch.setenv('SVI_SECRET_KEYS', json.dumps(keys))
        monkeypatch.setenv('SVI_SECRET_ACTIVE_KEY_ID', 'k1')
    from app.main import create_app
    return TestClient(create_app(str(path)))


@pytest.fixture()
def admin_app(clean_database, monkeypatch, tmp_path, keys):
    with start_app(monkeypatch, tmp_path, keys) as client:
        wait_for_readiness(client)
        yield client


def admin(client, email='admin@example.com'):
    client.app.state.register_limiter.reset()
    client.app.state.login_limiter.reset()
    register_and_login(client, email=email)
    execute_sql("UPDATE users SET role='admin' WHERE email=$1", email)
    client.app.state.login_limiter.reset()
    return auth_headers(register_login(client, email))


def register_login(client, email, client_type='admin_web'):
    response = client.post('/api/auth/login', json={'email': email, 'password': 'SmokePass123',
                                                     'client_type': client_type})
    assert response.status_code == 200, response.text
    return response.json()


def user(client, email='u1@example.com'):
    client.app.state.register_limiter.reset()
    client.app.state.login_limiter.reset()
    return auth_headers(register_and_login(client, email=email))


def asr_body(server, name='百炼云端识别', **kw):
    body = {'kind': 'asr', 'name': name, 'type': 'bailian_realtime', 'base_url': server.url if server else None,
            'model_id': 'qwen3-asr-flash-realtime', 'params': {}, 'api_key': KEY}
    body.update(kw)
    return body


def llm_body(name='整理模型A', **kw):
    body = {'kind': 'llm', 'name': name, 'type': 'mock', 'model_id': 'mock-llm', 'params': {}}
    body.update(kw)
    return body


def error(response):
    return response.json()['error']


def active_version(client, headers, kind):
    return client.get('/api/admin/providers/active', headers=headers).json()[kind]['version']


def activate(client, headers, kind, provider_id, **kw):
    body = {'kind': kind, 'provider_id': provider_id, 'version': active_version(client, headers, kind), **kw}
    return client.put('/api/admin/providers/active', json=body, headers=headers)


@pytest.fixture()
def cloud():
    clock = ScaledClock(1)
    with ThreadedMock(clock) as server:
        yield server


# ---- CFG ---------------------------------------------------------------------

def test_cfg01_ciphertext_and_aad(admin_app, keys, cloud):
    headers = admin(admin_app)
    created = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers)
    assert created.status_code == 201, created.text
    pid = created.json()['id']
    blob, key_id, last4 = sql('select secret_ciphertext, secret_key_id, secret_last4 from provider_configs where id=$1', pid)[0]
    assert KEY.encode() not in blob and KEY[:12].encode() not in blob
    assert (key_id, last4) == ('k1', KEY[-4:])
    box = SecretBox({k: base64.b64decode(v) for k, v in keys.items()}, 'k1')
    assert box.open(blob, 'k1', associated_data(pid, 'asr')) == KEY
    with pytest.raises(SecretError):
        box.open(blob, 'k1', associated_data(str(uuid.uuid4()), 'asr'))


def test_cfg02_no_plaintext_anywhere(admin_app, cloud, caplog):
    caplog.set_level(logging.DEBUG)
    headers = admin(admin_app)
    texts = []
    r = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers)
    pid = r.json()['id']
    texts += [r.text, admin_app.get('/api/admin/providers', headers=headers).text,
              admin_app.post(f'/api/admin/providers/{pid}/test', headers=headers).text,
              admin_app.patch(f'/api/admin/providers/{pid}', json={'version': 1, 'name': '改名'}, headers=headers).text,
              activate(admin_app, headers, 'asr', pid, confirm_cloud=True).text,
              admin_app.get('/api/admin/providers/active', headers=headers).text,
              admin_app.get('/api/admin/providers/audit', headers=headers).text]
    texts.append(str([tuple(r) for r in sql('select * from provider_audit')]))
    texts += [rec.getMessage() for rec in caplog.records if not rec.name.startswith('tests.')]
    joined = '\n'.join(texts)
    assert KEY not in joined and KEY[:-4] not in joined
    assert '"secret_set":true' in joined.replace(' ', '') and KEY[-4:] in joined


def test_cfg03_rotate_all_or_nothing(admin_app, keys, cloud):
    from app.cli import MaintenanceError, rotate_secrets
    headers = admin(admin_app)
    ids = [admin_app.post('/api/admin/providers', json=asr_body(cloud, name=f'云端{i}', api_key=f'{KEY}{i}'),
                          headers=headers).json()['id'] for i in range(3)]
    raw = {k: base64.b64decode(v) for k, v in keys.items()}
    box = SecretBox(raw, 'k1')
    db = admin_app.app.state.db

    def run(coro):
        return admin_app.portal.call(lambda: coro)

    with pytest.raises(MaintenanceError):
        run(rotate_secrets('k2', box=box, database=db, _fail_on=2))
    assert {r[0] for r in sql('select secret_key_id from provider_configs')} == {'k1'}
    for pid, i in zip(ids, range(3)):
        blob, kid = sql('select secret_ciphertext, secret_key_id from provider_configs where id=$1', pid)[0]
        assert SecretBox({'k1': raw['k1']}, 'k1').open(blob, kid, associated_data(pid, 'asr')) == f'{KEY}{i}'
    result = run(rotate_secrets('k2', box=box, database=db))
    assert result['changed'] == 3
    only_k2 = SecretBox({'k2': raw['k2']}, 'k2')  # k1 removed
    for pid in ids:
        blob, kid = sql('select secret_ciphertext, secret_key_id from provider_configs where id=$1', pid)[0]
        assert kid == 'k2' and only_k2.open(blob, kid, associated_data(pid, 'asr')).startswith(KEY)


def test_cfg04_missing_master_key_falls_back_to_yaml(clean_database, monkeypatch, tmp_path, keys, cloud):
    with start_app(monkeypatch, tmp_path, keys) as client:
        wait_for_readiness(client)
        headers = admin(client)
        pid = client.post('/api/admin/providers', json=asr_body(cloud), headers=headers).json()['id']
        assert activate(client, headers, 'asr', pid, confirm_cloud=True).status_code == 200
        assert client.get('/api/admin/providers/active', headers=headers).json()['asr']['source'] == 'db'
    with start_app(monkeypatch, tmp_path, None) as client:
        wait_for_readiness(client)
        headers = auth_headers(register_login(client, 'admin@example.com'))
        active = client.get('/api/admin/providers/active', headers=headers).json()['asr']
        assert (active['source'], active['source_reason']) == ('yaml', 'secret_unavailable')
        assert client.get('/healthz').json()['providers']['asr'] == {'source': 'yaml', 'reason': 'secret_unavailable'}
        assert client.app.state.asr.capabilities.provider_id != pid


def test_cfg05_production_without_any_asr(clean_database, monkeypatch, tmp_path, keys):
    monkeypatch.setenv('SVI_JWT_SECRET', secrets.token_urlsafe(48))
    monkeypatch.setenv('SVI_IDEMPOTENCY_SECRET', secrets.token_urlsafe(48))
    with start_app(monkeypatch, tmp_path, keys, environment='production', instance_lock=True,
                   extra={'llm': {'provider': 'ollama', 'base_url': 'http://127.0.0.1:1'}}) as client:
        try:
            wait_for_readiness(client, pending_reason='ASR_UNAVAILABLE')
        except AssertionError:
            pass
        assert client.app.state.asr is None  # YAML mock refused outside development
        headers = user(client)
        rid = str(uuid.uuid4())
        response = client.post('/api/v1/sessions', json={'client_request_id': rid, 'learning_enabled': True},
                               headers={**headers, 'Idempotency-Key': rid})
        assert response.status_code == 503
        assert error(response)['code'] == 'SERVICE_UNAVAILABLE'
        assert error(response)['details']['reason'] == 'asr_unavailable'


ALLOW = {'egress_policy': 'allowlist', 'egress_allowlist': ['*.maas.aliyuncs.com', 'api.deepseek.com']}


def fake_resolver(answer):
    async def resolve(host, port):
        return [answer]
    return resolve


@pytest.fixture()
def offline(monkeypatch):
    """No real network: cloud handshakes and LLM probes fail fast as unavailable."""
    async def refuse(uri, **kwargs):
        raise OSError('offline test')
    monkeypatch.setattr(bailian_conn, '_open_ws', refuse)
    from app.providers.asr.base import ProviderError
    from app.providers.llm.ollama import OllamaProvider
    from app.providers.llm.openai_compatible import OpenAICompatibleProvider

    async def unavailable(self):
        raise ProviderError('unavailable', 'offline test')
    monkeypatch.setattr(OpenAICompatibleProvider, 'prepare', unavailable)
    monkeypatch.setattr(OllamaProvider, 'prepare', unavailable)


def use_policy(client, mode, answer='8.8.8.8'):
    client.app.state.providers.egress = EgressPolicy(mode, ALLOW['egress_allowlist'], resolver=fake_resolver(answer))


def test_cfg06_address_allowlist(admin_app, offline):
    headers = admin(admin_app)
    use_policy(admin_app, 'allowlist')
    cases = [('asr', 'wss://ws-x.cn-beijing.maas.aliyuncs.com', True),
             ('asr', 'wss://maas.aliyuncs.com', False),
             ('asr', 'wss://evilmaas.aliyuncs.com', False),
             ('asr', 'wss://api.deepseek.com.attacker.io', False),
             ('llm', 'https://api.deepseek.com', True),
             ('llm', 'https://user:pw@api.deepseek.com', False),
             ('asr', 'wss://1.2.3.4', False),
             ('asr', 'ws://ws-x.cn-beijing.maas.aliyuncs.com', False)]
    for i, (kind, url, allowed) in enumerate(cases):
        body = (asr_body(None, name=f'c{i}', base_url=url) if kind == 'asr' else
                llm_body(f'c{i}', type='openai_compatible', base_url=url, model_id='deepseek-flash'))
        response = admin_app.post('/api/admin/providers', json=body, headers=headers)
        if allowed:
            assert response.status_code == 502, (url, response.text)  # passed egress, offline probe
        else:
            assert response.status_code == 422, (url, response.text)
            assert error(response)['details'] == {'fields': ['base_url'], 'reason': 'egress_denied'}
    assert sql('select count(*) from provider_configs')[0][0] == 0


@pytest.mark.parametrize('answer,denied', [('10.0.0.5', True), ('8.8.8.8', False)])
def test_cfg07_private_resolution(admin_app, offline, answer, denied):
    headers = admin(admin_app)
    use_policy(admin_app, 'allowlist', answer)
    response = admin_app.post('/api/admin/providers', json=asr_body(
        None, base_url='wss://ws-x.cn-beijing.maas.aliyuncs.com'), headers=headers)
    if denied:
        assert response.status_code == 422 and error(response)['details']['reason'] == 'egress_denied'
    else:
        assert response.status_code == 502  # resolution accepted, probe offline


@pytest.mark.parametrize('mode', ['deny-public', 'allowlist'])
def test_cfg08_internal_and_public_llm(admin_app, offline, mode):
    headers = admin(admin_app)
    use_policy(admin_app, mode)
    internal = admin_app.post('/api/admin/providers', json=llm_body(
        'intranet', type='ollama', base_url='http://10.0.0.8:11434', model_id='qwen'), headers=headers)
    assert internal.status_code == 502, internal.text  # address allowed; offline probe
    public = admin_app.post('/api/admin/providers', json=llm_body(
        'deepseek', type='openai_compatible', base_url='https://api.deepseek.com', model_id='deepseek-flash'),
        headers=headers)
    if mode == 'deny-public':
        assert public.status_code == 422 and error(public)['details']['reason'] == 'egress_denied'
    else:
        assert public.status_code == 502


def test_cfg09_redirect_not_followed(admin_app):
    headers = admin(admin_app)
    hits = []

    async def handler(reader, writer):
        hits.append(1)
        await reader.readuntil(b'\r\n\r\n')
        writer.write(b'HTTP/1.1 302 Found\r\nLocation: ws://203.0.113.9/evil\r\nContent-Length: 0\r\n\r\n')
        await writer.drain()
        writer.close()

    async def start():
        return await asyncio.start_server(handler, '127.0.0.1', 0)

    server = admin_app.portal.call(start)
    try:
        port = server.sockets[0].getsockname()[1]
        response = admin_app.post('/api/admin/providers', json=asr_body(None, base_url=f'ws://127.0.0.1:{port}'),
                                  headers=headers)
        assert response.status_code == 502 and error(response)['details']['reason'] == 'unavailable'
        assert len(hits) == 1  # the redirect target was never contacted
    finally:
        server.close()


@pytest.mark.parametrize('body,field', [
    ({'params': {'catchup_rate': 2.5}}, 'catchup_rate'),
    ({'params': {'segment_target_s': 10}}, 'segment_target_s'),
    ({'params': {'max_record_seconds': 361}}, 'max_record_seconds'),
    ({'type': 'mock', 'base_url': None, 'params': {'max_record_seconds': 181}}, 'max_record_seconds'),
    ({'params': {'max_reconnects': 21}}, 'max_reconnects'),
    ({'params': {'foo': 1}}, 'foo'),
    ({'kind': 'llm', 'type': 'mock', 'params': {'temperature': 0.7}}, 'temperature'),
])
def test_cfg10_param_allowlist(admin_app, cloud, body, field):
    headers = admin(admin_app)
    response = admin_app.post('/api/admin/providers', json=asr_body(cloud, **body), headers=headers)
    assert response.status_code == 422, response.text
    assert error(response)['code'] == 'VALIDATION_ERROR' and error(response)['details']['fields'] == [field]
    assert sql('select count(*) from provider_configs')[0][0] == 0


def test_cfg11_local_asr_on_linux(admin_app, monkeypatch):
    from app.services import provider_service
    monkeypatch.setattr(provider_service, 'local_asr_supported', lambda: False)
    headers = admin(admin_app)
    response = admin_app.post('/api/admin/providers', json={
        'kind': 'asr', 'name': '本机识别', 'type': 'qwen3_mlx', 'model_id': 'qwen3-asr', 'params': {}},
        headers=headers)
    assert response.status_code == 422
    assert error(response)['details'] == {'fields': ['type'], 'reason': 'host_unsupported'}


def test_cfg12_global_tables(admin_app):
    columns = {(t, c) for t, c in sql("select table_name, column_name from information_schema.columns "
                                     "where table_name in ('provider_configs','provider_active','provider_audit')")}
    assert columns and not any(c == 'user_id' for _, c in columns)
    assert sql("select count(*) from pg_constraint where conname='uq_provider_configs_kind_name'")[0][0] == 1
    headers = admin(admin_app)
    admin_app.get('/api/admin/providers/active', headers=headers)
    assert [r[0] for r in sql('select kind from provider_active order by kind')] == ['asr', 'llm']
    from tests.integration.test_v2_cloud_runtime import alembic
    alembic('upgrade', 'head')  # already at head: no change
    assert [r[0] for r in sql('select kind from provider_active order by kind')] == ['asr', 'llm']


def test_cfg13_no_secrets_in_repository():
    root = SERVER_ROOT.parent
    listed = subprocess.run(['git', 'ls-files'], cwd=root, capture_output=True, text=True)
    if listed.returncode == 0:
        files = listed.stdout.split()
    else:  # copied tree without Git metadata: walk it, skipping dependency caches
        skip = {'.git', 'node_modules', '.venv', '__pycache__', 'dist', 'temp'}
        files = [str(path.relative_to(root)) for path in root.rglob('*')
                 if path.is_file() and not skip & set(path.relative_to(root).parts)]
    import re
    key_like = re.compile(r'sk-(?!test-)[A-Za-z0-9]{20,}')
    # A real master key value: 32 bytes as base64 (44 chars) inside a key map.
    master = re.compile(r'"[A-Za-z0-9_-]{1,32}"\s*:\s*"[A-Za-z0-9+/]{43}="')
    leaks = []
    for name in files:
        if not name.endswith(('.py', '.yaml', '.yml', '.md', '.json', '.ts', '.tsx', '.env', '.example', '.toml')):
            continue
        try:
            text = (root / name).read_text(errors='ignore')
        except OSError:
            continue
        if key_like.search(text) or master.search(text):
            leaks.append(name)
    assert leaks == []


# ---- PAPI --------------------------------------------------------------------

ENDPOINTS = [('get', '/api/admin/providers'), ('post', '/api/admin/providers'),
             ('patch', '/api/admin/providers/00000000-0000-4000-8000-000000000000'),
             ('delete', '/api/admin/providers/00000000-0000-4000-8000-000000000000'),
             ('post', '/api/admin/providers/00000000-0000-4000-8000-000000000000/test'),
             ('put', '/api/admin/providers/active'), ('get', '/api/admin/providers/active'),
             ('get', '/api/admin/providers/audit')]


def test_papi01_authorization(admin_app):
    plain = user(admin_app)
    headers = admin(admin_app)
    for method, path in ENDPOINTS:
        kwargs = {'json': {}} if method in ('post', 'patch', 'put') else {}
        assert getattr(admin_app, method)(path, **kwargs).status_code == 401
        assert error(getattr(admin_app, method)(path, headers=plain, **kwargs))['code'] == 'FORBIDDEN'
        assert getattr(admin_app, method)(path, headers=headers, **kwargs).status_code not in (401, 403)
    execute_sql("UPDATE users SET role='user' WHERE email='admin@example.com'")
    assert admin_app.get('/api/admin/providers', headers=headers).status_code == 403


def test_papi02_create_cloud(admin_app, cloud):
    headers = admin(admin_app)
    response = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body['enabled'], body['is_active'], body['version'], body['last_test_ok']) == (True, False, 1, True)
    assert body['secret_set'] and body['secret_last4'] == KEY[-4:] and 'api_key' not in body
    assert len(cloud.connections) == 1 and sum(n for _, _, n in cloud.connections[0].appends) == 16000
    audit = sql('select action, result, changed_fields from provider_audit')
    assert len(audit) == 1 and audit[0][:2] == ('create', 'ok')
    assert KEY not in str(audit) and 'api_key' in audit[0][2]


def test_papi03_auth_failed_test(admin_app):
    clock = ScaledClock(1)
    with ThreadedMock(clock, handshake=lambda n: 401) as server:
        headers = admin(admin_app)
        response = admin_app.post('/api/admin/providers', json=asr_body(server), headers=headers)
    assert response.status_code == 502
    assert error(response)['code'] == 'PROVIDER_TEST_FAILED' and error(response)['details']['reason'] == 'auth_failed'
    assert sql('select count(*) from provider_configs')[0][0] == 0
    assert [tuple(r) for r in sql('select action, result, reason from provider_audit')] == [('create', 'failed', 'auth_failed')]


def test_papi04_duplicate_name(admin_app, cloud):
    headers = admin(admin_app)
    assert admin_app.post('/api/admin/providers', json=asr_body(cloud, name='同名'), headers=headers).status_code == 201
    dup = admin_app.post('/api/admin/providers', json=asr_body(cloud, name='同名'), headers=headers)
    assert dup.status_code == 409 and error(dup)['code'] == 'PROVIDER_NAME_EXISTS'
    assert error(dup)['details']['fields'] == ['name']
    assert admin_app.post('/api/admin/providers', json=llm_body('同名'), headers=headers).status_code == 201


def test_papi05_version_conflict(admin_app, cloud):
    headers = admin(admin_app)
    pid = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers).json()['id']
    assert admin_app.patch(f'/api/admin/providers/{pid}', json={'version': 1, 'name': 'v2'}, headers=headers).json()['version'] == 2
    first = admin_app.patch(f'/api/admin/providers/{pid}', json={'version': 2, 'name': 'A'}, headers=headers)
    second = admin_app.patch(f'/api/admin/providers/{pid}', json={'version': 2, 'name': 'B'}, headers=headers)
    assert first.json()['version'] == 3
    assert second.status_code == 409 and error(second)['code'] == 'VERSION_CONFLICT'
    assert error(second)['details']['current_version'] == 3
    assert sql('select name from provider_configs where id=$1', pid)[0][0] == 'A'


def test_papi06_api_key_semantics(admin_app, cloud):
    headers = admin(admin_app)
    pid = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers).json()['id']
    blob = sql('select secret_ciphertext from provider_configs where id=$1', pid)[0][0]
    r = admin_app.patch(f'/api/admin/providers/{pid}', json={'version': 1, 'name': 'n2'}, headers=headers)
    assert r.status_code == 200 and sql('select secret_ciphertext from provider_configs where id=$1', pid)[0][0] == blob
    cleared = admin_app.patch(f'/api/admin/providers/{pid}', json={'version': 2, 'api_key': ''}, headers=headers)
    assert cleared.status_code == 422 and error(cleared)['details']['fields'] == ['api_key']
    assert sql('select secret_ciphertext from provider_configs where id=$1', pid)[0][0] == blob
    tests_before = len(cloud.connections)
    new = admin_app.patch(f'/api/admin/providers/{pid}', json={'version': 2, 'api_key': KEY2}, headers=headers)
    assert new.status_code == 200 and new.json()['secret_last4'] == KEY2[-4:]
    assert len(cloud.connections) == tests_before + 1


def test_papi07_cloud_confirmation(admin_app, cloud):
    headers = admin(admin_app)
    local = admin_app.post('/api/admin/providers', json={'kind': 'asr', 'name': '本地', 'type': 'mock',
                                                          'model_id': 'mock-asr', 'params': {}}, headers=headers).json()['id']
    assert activate(admin_app, headers, 'asr', local).status_code == 200
    pid = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers).json()['id']
    denied = activate(admin_app, headers, 'asr', pid)
    assert denied.status_code == 422
    assert error(denied)['details'] == {'fields': ['confirm_cloud'], 'reason': 'cloud_confirmation_required'}
    assert sql("select provider_id from provider_active where kind='asr'")[0][0] == local
    ok = activate(admin_app, headers, 'asr', pid, confirm_cloud=True)
    assert ok.status_code == 200, ok.text
    row = sql("select provider_id, confirmed_cloud_at, confirmed_cloud_by from provider_active where kind='asr'")[0]
    assert row[0] == pid and row[1] is not None and row[2] is not None
    actions = [r[0] for r in sql("select action from provider_audit where result='ok' and action in ('confirm_cloud','activate') order by seq")]
    assert actions == ['activate', 'confirm_cloud', 'activate']
    assert activate(admin_app, headers, 'asr', local).status_code == 200
    assert activate(admin_app, headers, 'asr', pid).status_code == 200  # no second confirmation


def test_papi08_activation_test_failure_keeps_current(admin_app, cloud):
    headers = admin(admin_app)
    pid = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers).json()['id']
    current = admin_app.app.state.asr
    version = active_version(admin_app, headers, 'asr')
    cloud.handshake = lambda n: 429
    response = activate(admin_app, headers, 'asr', pid, confirm_cloud=True)
    assert response.status_code == 502 and error(response)['details']['reason'] == 'rate_limited'
    assert active_version(admin_app, headers, 'asr') == version and admin_app.app.state.asr is current


def test_papi09_conflicts(admin_app, cloud):
    headers = admin(admin_app)
    pid = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers).json()['id']
    assert activate(admin_app, headers, 'asr', pid, confirm_cloud=True).status_code == 200
    other = admin_app.post('/api/admin/providers', json=asr_body(cloud, name='备用', enabled=False), headers=headers).json()
    before = sql('select id, version, enabled from provider_configs order by id')
    assert error(admin_app.delete(f'/api/admin/providers/{pid}', headers=headers))['code'] == 'PROVIDER_ACTIVE'
    r = admin_app.patch(f'/api/admin/providers/{pid}', json={'version': 1, 'enabled': False}, headers=headers)
    assert r.status_code == 409 and error(r)['code'] == 'PROVIDER_ACTIVE'
    r = activate(admin_app, headers, 'asr', other['id'])
    assert r.status_code == 409 and error(r)['code'] == 'PROVIDER_DISABLED'
    assert admin_app.delete(f'/api/admin/providers/{uuid.uuid4()}', headers=headers).status_code == 404
    assert sql('select id, version, enabled from provider_configs order by id') == before


def test_papi10_delete(admin_app, cloud):
    headers = admin(admin_app)
    pid = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers).json()['id']
    assert admin_app.delete(f'/api/admin/providers/{pid}', headers=headers).status_code == 200
    assert sql('select count(*) from provider_configs where id=$1', pid)[0][0] == 0
    row = sql("select changed_fields, result from provider_audit where action='delete'")[0]
    assert json.loads(row[0]) == [] and row[1] == 'ok'


def test_papi11_reads(admin_app, cloud):
    headers = admin(admin_app)
    workspace = cloud.url.replace('127.0.0.1', '127.0.0.1')
    pid = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers).json()['id']
    activate(admin_app, headers, 'asr', pid, confirm_cloud=True)
    active = admin_app.get('/api/admin/providers/active', headers=headers).json()
    asr = active['asr']
    assert asr['source'] == 'db' and asr['provider']['id'] == pid
    assert asr['max_record_seconds'] == 360 and asr['max_concurrent_sessions'] == 8
    assert asr['provider']['last_test_ok'] is True and asr['provider']['secret_last4'] == KEY[-4:]
    items = admin_app.get('/api/admin/providers?kind=asr', headers=headers).json()['items']
    assert items[0]['secret_set'] and items[0]['base_url'] == workspace
    from app.services.provider_service import ProviderService
    view = ProviderService.view(None, type('R', (), {**{c: None for c in (
        'id', 'kind', 'name', 'type', 'model_id', 'params', 'enabled', 'secret_ciphertext', 'secret_last4',
        'last_test_at', 'last_test_ok', 'last_test_reason', 'version', 'updated_at')},
        'base_url': 'wss://ws-abc123.cn-beijing.maas.aliyuncs.com'})(), set())
    assert view['base_url'] == 'wss://ws-….cn-beijing.maas.aliyuncs.com'


def test_papi12_audit_pagination(admin_app, cloud):
    headers = admin(admin_app)
    pid = admin_app.post('/api/admin/providers', json=llm_body(), headers=headers).json()['id']
    for i in range(61):
        admin_app.patch(f'/api/admin/providers/{pid}', json={'version': i + 1, 'name': f'n{i}'}, headers=headers)
    first = admin_app.get('/api/admin/providers/audit?limit=50', headers=headers).json()
    second = admin_app.get(f"/api/admin/providers/audit?limit=50&cursor={first['next_cursor']}", headers=headers).json()
    assert len(first['items']) == 50 and len(second['items']) == 12 and second['next_cursor'] is None
    times = [i['at'] for i in first['items'] + second['items']]
    assert times == sorted(times, reverse=True)
    bad = admin_app.get('/api/admin/providers/audit?cursor=nope', headers=headers)
    assert bad.status_code == 422 and error(bad)['code'] == 'CURSOR_INVALID'
    assert all(set(i) >= {'changed_fields'} and 'name' in i['changed_fields'] for i in first['items'][:-1])


def test_papi13_test_endpoint(admin_app, cloud):
    headers = admin(admin_app)
    created = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers).json()
    ok = admin_app.post(f"/api/admin/providers/{created['id']}/test", headers=headers)
    assert ok.status_code == 200 and ok.json()['last_test_ok'] is True and ok.json()['version'] == 1
    cloud.handshake = lambda n: 503
    failed = admin_app.post(f"/api/admin/providers/{created['id']}/test", headers=headers)
    assert failed.status_code == 502 and error(failed)['details']['reason'] == 'unavailable'
    row = sql('select last_test_ok, last_test_reason, version, name from provider_configs where id=$1', created['id'])[0]
    assert tuple(row) == (False, 'unavailable', 1, created['name'])


# ---- LLM hot switch ------------------------------------------------------------

def dictation(client, admin_headers, pcm_seconds=2):
    from app.providers.asr.mock import MockAsrProvider
    headers = user(client, f'speaker-{uuid.uuid4().hex[:6]}@example.com')
    rid = str(uuid.uuid4())
    sid = client.post('/api/v1/sessions', json={'client_request_id': rid, 'learning_enabled': True},
                      headers={**headers, 'Idempotency-Key': rid}).json()['session_id']
    _ = MockAsrProvider
    return headers, sid, stream_audio(client, headers, sid, synth(pcm_seconds), ScaledClock(50))


def test_llm01_new_calls_use_new_provider(admin_app):
    headers = admin(admin_app)
    a = admin_app.post('/api/admin/providers', json=llm_body('A'), headers=headers).json()['id']
    b = admin_app.post('/api/admin/providers', json=llm_body('B'), headers=headers).json()['id']
    assert activate(admin_app, headers, 'llm', a).status_code == 200
    assert activate(admin_app, headers, 'llm', b).status_code == 200
    _, sid, events = dictation(admin_app, headers)
    assert events[-1]['type'] == 'result.ready', events[-1]
    rows = sql("select provider_config_id from model_calls where session_id=$1 and purpose='refine'", sid)
    assert rows and {r[0] for r in rows} == {b}


def test_llm02_in_flight_call_finishes_on_old(admin_app, monkeypatch):
    headers = admin(admin_app)
    a = admin_app.post('/api/admin/providers', json=llm_body('A'), headers=headers).json()['id']
    b = admin_app.post('/api/admin/providers', json=llm_body('B'), headers=headers).json()['id']
    assert activate(admin_app, headers, 'llm', a).status_code == 200
    old = admin_app.app.state.llm
    running = lambda: old.scheduler.snapshot()['running']
    original = MockLlmProvider.generate
    closed = []

    async def slow(self, request, deadline):
        if request.purpose == 'refine' and getattr(self, 'slow', False):
            await asyncio.sleep(3)
        return await original(self, request, deadline)

    monkeypatch.setattr(MockLlmProvider, 'generate', slow)
    old.inner.slow = True
    real_close = old.close

    async def tracked_close():
        closed.append(True)
        await real_close()
    old.close = tracked_close
    speaker, sid = None, None
    import threading
    result = {}

    def speak():
        result['out'] = dictation(admin_app, headers)

    thread = threading.Thread(target=speak)
    thread.start()
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not running():
        time.sleep(0.02)
    assert running() == 1
    assert activate(admin_app, headers, 'llm', b).status_code == 200
    assert not closed  # still leased by the running call
    thread.join(20)
    _, sid, events = result['out']
    assert events[-1]['type'] == 'result.ready'
    assert {r[0] for r in sql("select provider_config_id from model_calls where session_id=$1 and purpose='refine'", sid)} == {a}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not closed:
        time.sleep(0.05)
    assert closed == [True]
    _, sid2, _ = dictation(admin_app, headers)
    assert {r[0] for r in sql("select provider_config_id from model_calls where session_id=$1 and purpose='refine'", sid2)} == {b}


def test_llm03_public_refine_never_classifies(admin_app):
    from app.models import ProviderConfig
    from app.providers.llm.base import LlmRequest
    from app.providers.llm.routing import PurposeRoutedLlmProvider
    manager = admin_app.app.state.providers
    row = ProviderConfig(id=str(uuid.uuid4()), kind='llm', name='ds', type='openai_compatible',
                         base_url='https://api.deepseek.com', model_id='deepseek-flash', params={}, version=1)
    instance = manager.build_llm_row(row, KEY)
    assert isinstance(instance, PurposeRoutedLlmProvider) and instance.classify is None

    async def classify():
        request = LlmRequest('classify', 's', {'before': 'a', 'after': 'b'}, {'type': 'object'})
        return await instance.generate(request, asyncio.get_running_loop().time() + 5)

    response = admin_app.portal.call(classify)
    assert response.error is not None and response.error.code == 'unavailable'
    assert response.provider == 'unconfigured'


def test_llm04_failed_activation_keeps_llm(admin_app, monkeypatch):
    headers = admin(admin_app)
    a = admin_app.post('/api/admin/providers', json=llm_body('A'), headers=headers).json()['id']
    b = admin_app.post('/api/admin/providers', json=llm_body('B'), headers=headers).json()['id']
    assert activate(admin_app, headers, 'llm', a).status_code == 200
    current = admin_app.app.state.llm
    from app.providers.asr.base import ProviderError

    async def broken(self):
        raise ProviderError('unavailable', 'fixture')
    monkeypatch.setattr(MockLlmProvider, 'prepare', broken)
    response = activate(admin_app, headers, 'llm', b)
    assert response.status_code == 502 and error(response)['code'] == 'PROVIDER_TEST_FAILED'
    assert admin_app.app.state.llm is current
    assert admin_app.app.state.sessions.refiner.llm is current


def test_pui07_regression_cors_preflight_allows_put(admin_app):
    """Found by the real-browser PUI-07 run: the admin origin must be allowed to PUT
    /api/admin/providers/active (TestClient requests never send a preflight)."""
    response = admin_app.options('/api/admin/providers/active', headers={
        'Origin': 'http://127.0.0.1:17864', 'Access-Control-Request-Method': 'PUT',
        'Access-Control-Request-Headers': 'authorization,content-type'})
    assert response.status_code == 200, response.text
    assert 'PUT' in response.headers['access-control-allow-methods']


def test_activated_instance_keeps_reconnect_budget(admin_app, cloud):
    """Real-cloud finding 2026-10-28: the probe's max_reconnects=0 leaked into the
    activated instance, so one cloud disconnect failed a production session."""
    headers = admin(admin_app)
    pid = admin_app.post('/api/admin/providers', json=asr_body(cloud), headers=headers).json()['id']
    assert activate(admin_app, headers, 'asr', pid, confirm_cloud=True).status_code == 200
    assert admin_app.app.state.asr.config.max_reconnects == 8
