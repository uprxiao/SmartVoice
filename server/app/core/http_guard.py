"""Bounded ASGI request admission, including unknown-length/chunked bodies."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque

from starlette.websockets import WebSocket

from .ws_denial import deny_websocket


class AdmissionMetrics:
    """Bounded per-second counters; never drop events to cap a request deque."""
    def __init__(self):
        self.rejections = deque()

    def rejected(self):
        second = int(time.monotonic())
        self._trim(second)
        if self.rejections and self.rejections[-1][0] == second:
            timestamp, count = self.rejections.pop()
            self.rejections.append((timestamp, count + 1))
        else:
            self.rejections.append((second, 1))

    def _trim(self, now):
        while self.rejections and self.rejections[0][0] <= now - 300:
            self.rejections.popleft()

    def rejected_count(self):
        self._trim(int(time.monotonic()))
        return sum(count for _, count in self.rejections)


class HttpGuard:
    def __init__(self, app, owner):
        self.app, self.owner = app, owner

    async def _run_owned(self, scope, receive, send):
        state = self.owner.state
        requests = getattr(state, "active_requests", None)
        if requests is None:
            requests = state.active_requests = set()
        done = asyncio.get_running_loop().create_future()
        requests.add(done)
        corrections = None
        if scope["type"] == "http" and scope.get("method") == "POST" and scope.get("path") == "/api/v1/corrections":
            corrections = getattr(state, "correction_requests", None)
            if corrections is None:
                corrections = state.correction_requests = set()
            corrections.add(done)
        try:
            return await self.app(scope, receive, send)
        finally:
            done.set_result(None)
            requests.discard(done)
            if corrections is not None:
                corrections.discard(done)

    async def __call__(self, scope, receive, send):
        kind, path = scope["type"], scope.get("path", "")
        state = self.owner.state
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if kind == "websocket":
            if (getattr(state, "stopping", False) or not getattr(state, "ready", False)
                    or getattr(state, "instance_fenced", False)):
                await deny_websocket(WebSocket(scope, receive, send), 503,
                                     "SERVICE_UNAVAILABLE", "语音服务尚未就绪", retryable=True)
                return
            return await self._run_owned(scope, receive, send)
        headers = {}
        for key, value in scope.get("headers", []):
            headers.setdefault(key.lower(), []).append(value)
        proposed = headers.get(b"x-request-id", [b""])[0]
        request_id = (proposed.decode("ascii") if proposed and len(proposed) <= 128
                      and all(33 <= c <= 126 for c in proposed) else str(uuid.uuid4()))
        scope.setdefault("state", {})["request_id"] = request_id

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                response_headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                message = {**message, "headers": response_headers + [(b"x-request-id", request_id.encode())]}
                if message["status"] == 429 and path.startswith("/api/v1/sessions"):
                    metrics = getattr(state, "admission_metrics", None)
                    if metrics is not None:
                        metrics.rejected()
            await send(message)

        async def reject(status, code, message, details=None):
            body = json.dumps({"error": {"code": code, "message": message,
                "request_id": request_id, "retryable": status == 503, "details": details or {}}}).encode()
            await guarded_send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})

        if scope.get("method") != "OPTIONS" and path.startswith("/api/"):
            recovery = getattr(state, "startup_recovery_complete", None)
            if (getattr(state, "stopping", False) or getattr(state, "instance_fenced", False)
                or not getattr(state, "schema_ok", False)
                or not getattr(state, "database_ok", False)
                or (recovery is not None and (not recovery.done() or recovery.cancelled()))):
                return await reject(503, "SERVICE_UNAVAILABLE", "服务尚未就绪")
            sessions_path = path.startswith("/api/v1/sessions")
            if (sessions_path or path == "/api/v1/history") and getattr(state, "sessions", None) is None:
                return await reject(503, "SERVICE_UNAVAILABLE", "语音服务尚未就绪")
            if (scope.get("method") == "POST" and path == "/api/v1/corrections"
                    and getattr(state, "classifier", None) is None):
                return await reject(503, "SERVICE_UNAVAILABLE", "整理服务尚未就绪")
            new_attempt = scope.get("method") == "POST" and (path == "/api/v1/sessions" or
                                                                         (sessions_path and path.endswith("/retry")))
            if new_attempt and not getattr(state, "ready", False):
                # V2 §8.4: name a missing ASR explicitly (never a silent mock).
                details = ({"reason": "asr_unavailable"}
                           if getattr(state, "unavailable_reason", None) == "ASR_UNAVAILABLE" else None)
                return await reject(503, "SERVICE_UNAVAILABLE", "语音服务尚未就绪", details)

        importing = path == "/api/v1/dictionary/import"
        batch_deleting = scope.get("method") == "POST" and path == "/api/v1/dictionary/terms/batch-delete"
        limit = 2 * 1024 * 1024 if importing else 512 * 1024 if batch_deleting else 64 * 1024
        lengths = headers.get(b"content-length", [])
        if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or len(lengths[0]) > 16)):
            return await reject(400, "INVALID_JSON", "请求长度无效")
        if lengths and int(lengths[0]) > limit:
            return await reject(413, "REQUEST_TOO_LARGE", "请求体超过接口上限")
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                return await reject(413, "REQUEST_TOO_LARGE", "请求体超过接口上限")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        if lengths and size != int(lengths[0]):
            return await reject(400, "INVALID_JSON", "请求长度不一致")
        if size:
            content_types = headers.get(b"content-type", [])
            media_type = content_types[0].split(b";", 1)[0].strip().lower() if len(content_types) == 1 else b""
            expected = b"multipart/form-data" if importing else b"application/json"
            if media_type != expected:
                return await reject(415, "UNSUPPORTED_MEDIA_TYPE", "请求媒体类型无效")
        body, delivered = b"".join(chunks), False
        chunks.clear()
        async def replay():
            nonlocal delivered, body
            if not delivered:
                delivered = True
                result, body = body, b""
                return {"type": "http.request", "body": result, "more_body": False}
            return await receive()
        # No application/DB resources are acquired while an incomplete body is
        # being collected. Recheck the cutoff before entering the application.
        if path.startswith("/api/"):
            if getattr(state, "stopping", False) or getattr(state, "instance_fenced", False):
                return await reject(503, "SERVICE_UNAVAILABLE", "服务正在停止")
            return await self._run_owned(scope, replay, guarded_send)
        await self.app(scope, replay, guarded_send)
