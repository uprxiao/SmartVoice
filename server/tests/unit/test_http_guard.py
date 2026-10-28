"""ASGI-only regression tests. These do not prove provider or CAP20 acceptance."""
import asyncio
import json
from types import SimpleNamespace

from app.core.http_guard import AdmissionMetrics, HttpGuard


def request(chunks, *, headers=(), path='/api/auth/login', state=None):
    async def run():
        messages, reached = [], []
        pending = list(chunks)
        async def receive():
            return {'type': 'http.request', 'body': pending.pop(0), 'more_body': bool(pending)}
        async def send(message):
            messages.append(message)
        async def downstream(scope, receive, send):
            reached.append(await receive())
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        current = state or SimpleNamespace(schema_ok=True, database_ok=True, ready=True, sessions=object())
        guard = HttpGuard(downstream, SimpleNamespace(state=current))
        await guard({'type': 'http', 'path': path, 'method': 'POST', 'headers': list(headers)}, receive, send)
        return messages, reached
    return asyncio.run(run())


def test_chunked_body_exceeds_limit_before_route():
    messages, reached = request([b'a' * 32768, b'b' * 32769], headers=[(b'content-type', b'application/json')])
    assert not reached
    assert messages[0]['status'] == 413
    assert json.loads(messages[-1]['body'])['error']['code'] == 'REQUEST_TOO_LARGE'


def test_only_batch_delete_has_a_bounded_512k_json_allowance():
    headers = [(b'content-type', b'application/json')]
    path = '/api/v1/dictionary/terms/batch-delete'
    messages, reached = request([b' ' * (512 * 1024 - 2), b'{}'], headers=headers, path=path)
    assert messages[0]['status'] == 200 and len(reached[0]['body']) == 512 * 1024
    messages, reached = request([b' ' * (512 * 1024), b'{}'], headers=headers, path=path)
    assert messages[0]['status'] == 413 and not reached
    for other in ['/api/v1/dictionary/terms', path + '/extra']:
        messages, reached = request([b' ' * (64 * 1024), b'{}'], headers=headers, path=other)
        assert messages[0]['status'] == 413 and not reached


def test_chunked_wrong_media_type_rejected_without_content_length():
    messages, reached = request([b'{}'], headers=[(b'content-type', b'text/plain')])
    assert messages[0]['status'] == 415
    assert not reached


def test_exact_bound_body_is_replayed_once_and_has_request_id():
    messages, reached = request([b' ' * 65534, b'{}'], headers=[(b'content-type', b'application/json')])
    assert messages[0]['status'] == 200
    assert reached[0]['body'] == b' ' * 65534 + b'{}'
    assert dict(messages[0]['headers'])[b'x-request-id']


def test_bad_length_rejected_even_with_small_actual_body():
    messages, reached = request([b'{}'], headers=[(b'content-length', b'3'), (b'content-type', b'application/json')])
    assert messages[0]['status'] == 400
    assert json.loads(messages[-1]['body'])['error']['code'] == 'INVALID_JSON'
    assert not reached


def test_capacity_not_ready_preserves_auth_but_rejects_new_voice():
    state = SimpleNamespace(schema_ok=True, database_ok=True, ready=False, sessions=object())
    messages, _ = request([b'{}'], headers=[(b'content-type', b'application/json')], state=state)
    assert messages[0]['status'] == 200
    messages, reached = request([b'{}'], headers=[(b'content-type', b'application/json')], path='/api/v1/sessions', state=state)
    assert messages[0]['status'] == 503 and not reached


def test_database_or_ownership_loss_fails_closed():
    for field in ('database_ok', 'instance_fenced'):
        state = SimpleNamespace(schema_ok=True, database_ok=True, ready=True, sessions=object(), instance_fenced=False)
        setattr(state, field, field == 'instance_fenced')
        messages, reached = request([b'{}'], headers=[(b'content-type', b'application/json')], state=state)
        assert messages[0]['status'] == 503 and not reached


def test_rejection_metrics_count_all_events_and_expire(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr('app.core.http_guard.time.monotonic', lambda: now[0])
    metrics = AdmissionMetrics()
    for _ in range(10001):
        metrics.rejected()
    assert metrics.rejected_count() == 10001
    assert len(metrics.rejections) == 1
    now[0] += 300
    assert metrics.rejected_count() == 0
