"""Bailian realtime WebSocket transport and error mapping (V2 design §3.2, §3.5).

Nothing here logs or raises with the API key, the full URL or raw cloud
payloads; callers only see a ``CloudError`` code.
"""
from __future__ import annotations

import base64
import json
import logging
import ssl
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import websockets
import websockets.asyncio.client
from websockets.exceptions import InvalidHandshake, InvalidStatus

from ...core.egress import EgressDenied, EgressPolicy

REALTIME_PATH = '/api-ws/v1/realtime'
# Codes the session treats as recoverable by reconnecting (design §3.5).
RECOVERABLE = frozenset({'unavailable'})
# websockets logs full handshake headers (incl. Authorization) at DEBUG; pin its
# logger above DEBUG so no deployment log level can expose the API key.
TRANSPORT_LOG = logging.getLogger('svi.asr.bailian.transport')
TRANSPORT_LOG.setLevel(logging.WARNING)


class CloudError(Exception):
    def __init__(self, code: str, detail: str = ''):
        super().__init__(code)
        self.code = code
        self.detail = detail  # short, already-sanitised diagnostic token


def map_handshake_status(status: int) -> str:
    if status in (401, 403):
        return 'auth_failed'
    if status == 429:
        return 'rate_limited'
    if 400 <= status < 500:
        return 'bad_request'
    return 'unavailable'


def map_error_event(event: dict) -> CloudError:
    error = event.get('error') if isinstance(event.get('error'), dict) else {}
    kind = str(error.get('type') or '')
    code = str(error.get('code') or '')
    message = str(error.get('message') or '')
    probe = f'{kind} {code} {message}'.lower()
    if any(word in probe for word in ('unauthorized', 'invalidapikey', 'invalid api key', 'accessdenied',
                                      'authentication', 'forbidden')):
        return CloudError('auth_failed', 'error_event')
    if any(word in probe for word in ('throttl', 'ratelimit', 'rate limit', 'rate_limit', 'quota',
                                      'too many', 'concurrency')):
        return CloudError('rate_limited', 'error_event')
    if kind == 'invalid_request_error':
        return CloudError('bad_request', 'invalid_request_error')
    return CloudError('unavailable', 'error_event')


def realtime_url(base_url: str, model_id: str) -> str:
    parts = urlsplit(base_url)
    path = parts.path if parts.path not in ('', '/') else REALTIME_PATH
    return urlunsplit((parts.scheme, parts.netloc, path, f'model={quote(model_id)}', ''))


def session_update(language: str) -> str:
    transcription = {} if language in ('', 'auto') else {'language': language}
    # Manual commit only; no corpus/hotword fields are ever sent (FR-02-11).
    return json.dumps({'type': 'session.update', 'session': {
        'modalities': ['text'], 'input_audio_format': 'pcm', 'sample_rate': 16000,
        'input_audio_transcription': transcription, 'turn_detection': None}})


def append_message(pcm_s16le: bytes) -> str:
    return json.dumps({'type': 'input_audio_buffer.append',
                       'audio': base64.b64encode(pcm_s16le).decode()})


COMMIT_MESSAGE = json.dumps({'type': 'input_audio_buffer.commit'})
FINISH_MESSAGE = json.dumps({'type': 'session.finish'})


@dataclass
class ConnectSettings:
    base_url: str
    model_id: str
    api_key: str
    connect_timeout_s: float
    use_env_proxy: bool = False


class _NoRedirectConnect(websockets.asyncio.client.connect):
    """Never follow redirects (§6.3(4)): a 3xx handshake is a failed handshake."""

    def process_redirect(self, exc):
        return exc


async def _open_ws(uri: str, **kwargs):
    """Seam for tests; production always goes through this connector."""
    return await _NoRedirectConnect(uri, **kwargs)


async def connect(settings: ConnectSettings, egress: EgressPolicy):
    """Open one realtime socket to a freshly validated, pinned address."""
    try:
        target = egress.check_url(settings.base_url, kind='asr')
        address = await egress.resolve(target)
    except EgressDenied as exc:
        raise CloudError('egress_denied', 'policy') from exc
    tls = target.scheme == 'wss'
    kwargs = dict(
        additional_headers={'Authorization': f'Bearer {settings.api_key}', 'OpenAI-Beta': 'realtime=v1'},
        max_size=None, open_timeout=settings.connect_timeout_s, close_timeout=2,
        ping_interval=20, ping_timeout=20,
        proxy=True if settings.use_env_proxy else None,
        logger=TRANSPORT_LOG,
    )
    if not settings.use_env_proxy:
        # Direct connection to the validated IP; no second DNS lookup. With an
        # (opt-in) environment proxy the proxy resolves and pinning is lost.
        kwargs.update(host=address, port=target.port)
    if tls:
        # SNI and certificate verification use the configured host name.
        kwargs.update(ssl=ssl.create_default_context(), server_hostname=target.host)
    try:
        return await _open_ws(realtime_url(settings.base_url, settings.model_id), **kwargs)
    except InvalidStatus as exc:
        raise CloudError(map_handshake_status(exc.response.status_code), 'handshake') from exc
    except (InvalidHandshake, OSError, TimeoutError, EOFError) as exc:
        raise CloudError('unavailable', type(exc).__name__) from exc
