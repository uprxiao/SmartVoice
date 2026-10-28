"""HTTP error responses before the WebSocket upgrade; never a post-upgrade close."""
from __future__ import annotations

import uuid

from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketState


def _request_id(websocket: WebSocket) -> str:
    # Match HttpGuard's bounded printable-ASCII request ID policy. That guard
    # currently bypasses HTTP request-ID setup on websocket scopes.
    proposed = websocket.scope.setdefault("state", {}).get("request_id")
    if proposed is None:
        proposed = websocket.headers.get("x-request-id")
    if not (
        isinstance(proposed, str)
        and 1 <= len(proposed) <= 128
        and all(33 <= ord(char) <= 126 for char in proposed)
    ):
        proposed = str(uuid.uuid4())
    websocket.scope["state"]["request_id"] = proposed
    return proposed


async def deny_websocket(
    websocket: WebSocket,
    status: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """Send the ordinary JSON error envelope without accepting the socket.

    The configured ASGI server must support websocket.http.response (the pinned
    Uvicorn backends and Starlette TestClient do). Do not fall back to close():
    before accept that silently turns 401/404/409 into an HTTP 403 in Uvicorn.
    Already accepted sockets must continue to use their error event/440x path.
    """
    if websocket.application_state is not WebSocketState.CONNECTING:
        raise RuntimeError("WebSocket HTTP denial is only valid before upgrade")
    request_id = _request_id(websocket)
    response_headers = {
        key: value for key, value in (headers or {}).items()
        if key.lower() != "x-request-id"
    }
    response_headers["X-Request-Id"] = request_id
    await websocket.send_denial_response(
        JSONResponse(
            status_code=status,
            headers=response_headers,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": request_id,
                    "retryable": bool(retryable),
                    "details": details or {},
                }
            },
        )
    )
