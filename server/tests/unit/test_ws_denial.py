from __future__ import annotations

import json
import uuid

import pytest
from starlette.websockets import WebSocket

from app.core.ws_denial import deny_websocket


def socket(messages, *, headers=(), extension=True):
    scope = {
        "type": "websocket", "path": "/ws/test", "headers": list(headers),
        "state": {}, "extensions": {"websocket.http.response": {}} if extension else {},
    }
    async def receive():
        return {"type": "websocket.connect"}
    async def send(message):
        messages.append(message)
    return WebSocket(scope, receive, send)


@pytest.mark.parametrize("proposed", [None, b"", b"contains space", b"bad\r\nheader", b"x" * 129, b"\xff"])
async def test_denial_has_bounded_generated_request_id_without_echoing_invalid_header(proposed):
    messages = []
    headers = [] if proposed is None else [(b"x-request-id", proposed)]
    ws = socket(messages, headers=headers)
    await deny_websocket(ws, 401, "AUTH_INVALID", "登录状态无效，请重新登录")
    assert [m["type"] for m in messages] == ["websocket.http.response.start", "websocket.http.response.body"]
    assert messages[0]["status"] == 401
    error = json.loads(messages[1]["body"])["error"]
    assert error["code"] == "AUTH_INVALID"
    assert uuid.UUID(error["request_id"]).version == 4
    assert dict(messages[0]["headers"])[b"x-request-id"].decode() == error["request_id"]
    assert ws.scope["state"]["request_id"] == error["request_id"]


async def test_denial_preserves_valid_request_id_and_auth_error_details_without_upgrade():
    messages = []
    ws = socket(messages, headers=[(b"x-request-id", b"test-denial-123")])
    await deny_websocket(ws, 403, "ACCOUNT_DISABLED", "账号已禁用，请联系管理员")
    assert messages[0]["status"] == 403
    assert json.loads(messages[1]["body"]) == {"error": {
        "code": "ACCOUNT_DISABLED", "message": "账号已禁用，请联系管理员",
        "request_id": "test-denial-123", "retryable": False, "details": {},
    }}
    assert not any(m["type"] in {"websocket.accept", "websocket.close"} for m in messages)


async def test_denial_cannot_replace_an_established_websocket_close():
    messages = []
    ws = socket(messages)
    await ws.accept()
    before = list(messages)
    with pytest.raises(RuntimeError, match="only valid before upgrade"):
        await deny_websocket(ws, 409, "SESSION_STATE_CONFLICT", "状态冲突")
    assert messages == before
    # The established protocol remains available and retains its close code.
    await ws.close(code=4400)
    assert messages[-1]["type"] == "websocket.close"
    assert messages[-1]["code"] == 4400


async def test_missing_denial_extension_never_silently_accepts_or_changes_status_to_403():
    messages = []
    ws = socket(messages, extension=False)
    with pytest.raises(RuntimeError, match="Denial Response extension"):
        await deny_websocket(ws, 404, "NOT_FOUND", "会话不存在")
    assert messages == []
