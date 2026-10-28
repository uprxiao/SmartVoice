import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import asyncpg

from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, TEST_DB, auth_headers, register_and_login
from tests.test_dictionary import create_term, key


def post(client, headers, items, idem=None):
    return client.post('/api/v1/dictionary/terms/batch-delete', json={'items': items},
                       headers={**headers, 'Idempotency-Key': idem or key()})


def target(term):
    return {'id': term['id'], 'version': term['version']}


def snapshot(uid):
    async def read():
        c = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database=TEST_DB)
        try:
            return {
                'terms': [dict(r) for r in await c.fetch('select id,version,deleted_at is not null as deleted from dictionary_terms where user_id=$1 order by id', uid)],
                'version': await c.fetchval('select version from dictionary_state where user_id=$1', uid),
                'operations': await c.fetchval("select count(*) from operation_requests where user_id=$1 and operation='dictionary.terms.batch-delete'", uid),
                'learning': await c.fetchval('select count(*) from learning_events where user_id=$1', uid),
            }
        finally:
            await c.close()
    return asyncio.run(read())


def test_atomic_delete_only_selected_and_one_dictionary_increment(app_client):
    tokens = register_and_login(app_client); headers = auth_headers(tokens)
    terms = [create_term(app_client, headers, name).json() for name in ['Alpha', 'Beta', 'Gamma']]
    result = post(app_client, headers, [target(x) for x in terms[:2]])
    assert result.status_code == 200, result.text
    assert result.json() == {'deleted_ids': sorted(x['id'] for x in terms[:2]), 'dictionary_version': 4, 'replayed': False}
    actual = snapshot(tokens['user']['id'])
    assert actual['version'] == 4 and actual['operations'] == 1 and actual['learning'] == 0
    for row in actual['terms']:
        selected = row['id'] in result.json()['deleted_ids']
        assert row['deleted'] == selected and row['version'] == (2 if selected else 1)
    assert [x['term'] for x in app_client.get('/api/v1/dictionary/terms', headers=headers).json()['items']] == ['Gamma']


def test_batch_missing_foreign_deleted_and_version_conflicts_are_atomic(app_client):
    tokens = register_and_login(app_client); headers = auth_headers(tokens)
    other = register_and_login(app_client, email='other-batch@example.test'); foreign_headers = auth_headers(other)
    a, b = [create_term(app_client, headers, name).json() for name in ['Alpha', 'Beta']]
    foreign = create_term(app_client, foreign_headers, 'Foreign').json()
    original = snapshot(tokens['user']['id']); idem = key()
    for invalid in [{'id': key(), 'version': 1}, target(foreign)]:
        response = post(app_client, headers, [target(a), invalid], idem)
        assert response.status_code == 404 and response.json()['error']['code'] == 'NOT_FOUND'
        assert snapshot(tokens['user']['id']) == original
    assert app_client.patch(f"/api/v1/dictionary/terms/{b['id']}", json={'version': 1, 'term': 'Updated'}, headers={**headers, 'Idempotency-Key': key()}).status_code == 200
    updated = snapshot(tokens['user']['id'])
    conflict = post(app_client, headers, [target(a), target(b)], idem)
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'VERSION_CONFLICT'
    assert snapshot(tokens['user']['id']) == updated
    assert post(app_client, headers, [target(a), {'id': b['id'], 'version': 2}], idem).status_code == 200
    after = snapshot(tokens['user']['id'])
    assert post(app_client, headers, [target(a)], key()).status_code == 404
    assert snapshot(tokens['user']['id']) == after
    assert snapshot(other['user']['id'])['terms'] == [{'id': foreign['id'], 'version': 1, 'deleted': False}]


def test_same_key_permutation_replays_and_other_payload_conflicts(app_client):
    tokens = register_and_login(app_client); headers = auth_headers(tokens)
    terms = [create_term(app_client, headers, name).json() for name in ['Alpha', 'Beta']]
    items = [target(x) for x in terms]; idem = key()
    assert post(app_client, headers, items, idem).status_code == 200
    after = snapshot(tokens['user']['id'])
    replay = post(app_client, headers, list(reversed(items)), idem)
    assert replay.status_code == 200 and replay.json()['replayed'] is True
    assert snapshot(tokens['user']['id']) == after
    conflict = post(app_client, headers, items[:1], idem)
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    # A new same-name term is a different identity and cannot be deleted by replay.
    new = create_term(app_client, headers, 'Alpha').json()
    assert post(app_client, headers, items, idem).json()['replayed'] is True
    assert next(x for x in snapshot(tokens['user']['id'])['terms'] if x['id'] == new['id'])['deleted'] is False


def test_duplicate_concurrent_request_writes_once(app_client):
    tokens = register_and_login(app_client); headers = auth_headers(tokens)
    terms = [create_term(app_client, headers, name).json() for name in ['Alpha', 'Beta']]
    idem = key(); items = [target(x) for x in terms]
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: post(app_client, headers, items, idem), range(2)))
    assert [r.status_code for r in replies] == [200, 200]
    assert sorted(r.json()['replayed'] for r in replies) == [False, True]
    assert snapshot(tokens['user']['id'])['version'] == 3
    assert all(r['version'] == 2 for r in snapshot(tokens['user']['id'])['terms'])


def test_invalid_batch_shapes_are_zero_write(app_client):
    tokens = register_and_login(app_client); headers = auth_headers(tokens)
    valid = {'id': key(), 'version': 1}; original = snapshot(tokens['user']['id'])
    invalid = [[], [valid] * 5001, [valid, {**valid, 'id': valid['id'].upper()}],
               [{'id': 'invalid', 'version': 1}], [{'id': 12, 'version': 1}],
               [{**valid, 'version': version} for version in [0]], [{**valid, 'version': 1.5}],
               [{**valid, 'version': True}], [{**valid, 'user_id': key()}]]
    for items in invalid:
        result = post(app_client, headers, items)
        assert result.status_code == 422, result.text
        assert snapshot(tokens['user']['id']) == original
    missing_key = app_client.post('/api/v1/dictionary/terms/batch-delete', json={'items': [valid]}, headers=headers)
    assert missing_key.status_code == 422


def test_maximum_5000_items_deletes_atomically(app_client):
    tokens = register_and_login(app_client); headers = auth_headers(tokens); uid = tokens['user']['id']
    ids = [key() for _ in range(5000)]; now = datetime.now(timezone.utc)
    async def seed():
        c = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database=TEST_DB)
        try:
            await c.execute('insert into dictionary_state(user_id,version,updated_at) values($1,0,$2) on conflict(user_id) do nothing', uid, now)
            await c.copy_records_to_table('dictionary_terms', columns=['id', 'user_id', 'term', 'norm', 'aliases', 'contexts', 'scope', 'source', 'version', 'created_at', 'updated_at'], records=[(tid, uid, f'Term{i}', f'term{i}', '[]', '[]', 'default', 'manual', 1, now, now) for i, tid in enumerate(ids)])
        finally:
            await c.close()
    asyncio.run(seed())
    result = post(app_client, headers, [{'id': tid, 'version': 1} for tid in ids])
    assert result.status_code == 200, result.text
    assert len(result.json()['deleted_ids']) == 5000 and result.json()['dictionary_version'] == 1
    actual = snapshot(uid)
    assert len(actual['terms']) == 5000 and actual['version'] == 1 and actual['operations'] == 1
    assert all(row['deleted'] and row['version'] == 2 for row in actual['terms'])
