"""Assert HTTP handshake rejection separately from established-WS close frames."""
from __future__ import annotations

import pytest
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect


def assert_handshake_denied(client, path, headers, status, code):
    # Cancellation, WebSocketDisconnect, or a successful upgrade are not an
    # equivalent outcome. Only the HTTP denial response satisfies this check.
    with pytest.raises(WebSocketDenialResponse) as raised:
        with client.websocket_connect(path, headers=headers or {}):
            pytest.fail("denied handshake unexpectedly upgraded")
    response = raised.value
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"code", "message", "request_id", "retryable", "details"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert error["retryable"] is False
    assert error["details"] == {}
    assert response.headers["x-request-id"] == error["request_id"]
    assert 1 <= len(error["request_id"]) <= 128
    assert all(33 <= ord(char) <= 126 for char in error["request_id"])
    return response


def cancel_and_drain(ws):
    """End an accepted control connection normally, without swallowing failures."""
    ws.send_json({"type": "cancel"})
    for _ in range(256):
        event = ws.receive_json()
        if event["type"] == "cancelled":
            break
        assert event["type"] in {"audio.ack", "asr.partial"}
    else:
        pytest.fail("cancel terminal event was not observed within the bounded queue")
    with pytest.raises(WebSocketDisconnect) as raised:
        ws.receive_json()
    assert not isinstance(raised.value, WebSocketDenialResponse)
    assert raised.value.code == 1000
