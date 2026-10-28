"""Mock of the Bailian realtime ASR WebSocket (S3 fixture F-V2-MOCK).

Fictional data only. Test audio carries an absolute block marker in the
first sample of every 250 ms block (see ``tests.bailian_audio``), so each
``completed`` transcript is ``seg[<first block>-<last block>]`` and tests can
verify concatenation and resend without any speech model.

Time is the shared (scaled) test clock: injections and the overspeed rule
("more than 3× real time in any 2 s window → drop the excess and reject the
next commit", batch 08) are evaluated in audio seconds, not wall seconds.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus

import numpy as np
from websockets.asyncio.server import serve

SAMPLE_RATE = 16_000
BLOCK = 4_000
MOCK_LOGGER = 'tests.mock_bailian'  # test-side server logs; not product logs
CLOSE_1001_REASON = 'going away: Service unavailable (upstream_reset_after_response_started{connection_termination})'


@dataclass
class ConnRecord:
    index: int
    headers: dict
    path: str
    opened_at: float
    session_updates: list = field(default_factory=list)
    appends: list = field(default_factory=list)  # (clock time, first block marker, samples)
    audio: bytearray = field(default_factory=bytearray)
    commits: list = field(default_factory=list)  # (start_sample, end_sample) within this connection
    errors_sent: list = field(default_factory=list)
    overspeed_drops: int = 0
    closed_at: float | None = None
    close_code: int | None = None

    @property
    def first_marker(self) -> int | None:
        return self.appends[0][1] if self.appends else None


def marker_of(pcm: bytes) -> int:
    return int(np.frombuffer(pcm[:2], dtype='<i2')[0])


def default_text(audio: bytes, start: int, end: int) -> str:
    if end <= start:
        return ''
    first = marker_of(audio[start * 2:start * 2 + 2])
    last_block_start = start + (end - 1 - start) // BLOCK * BLOCK
    last = marker_of(audio[last_block_start * 2:last_block_start * 2 + 2])
    return f'seg[{first}-{last}]'


class MockBailianServer:
    def __init__(self, clock, *,
                 handshake: Callable[[int], int | None] | None = None,
                 refuse_after: float | None = None,
                 refuse_between: tuple[float, float] | None = None,
                 drop_at: tuple[float, ...] = (),
                 close_1001_at: tuple[float, ...] = (),
                 drop_after_commit: dict[int, float] | None = None,
                 error_on_commit: dict[int, str] | None = None,
                 never_complete: bool = False,
                 complete_delay: float = 0.2,
                 partial_events: bool = True,
                 revise_partials: bool = False,
                 text_fn: Callable[[bytes, int, int], str] = default_text,
                 overspeed_factor: float | None = 3.0):
        self.clock = clock
        self.handshake = handshake
        self.refuse_after = refuse_after
        self.refuse_between = refuse_between
        self.pending_drops = sorted(drop_at)
        self.pending_1001 = sorted(close_1001_at)
        self.drop_after_commit = dict(drop_after_commit or {})
        self.error_on_commit = dict(error_on_commit or {})
        self.never_complete = never_complete
        self.complete_delay = complete_delay
        self.partial_events = partial_events
        self.revise_partials = revise_partials
        self.text_fn = text_fn
        self.overspeed_factor = overspeed_factor
        self.connections: list[ConnRecord] = []
        self.handshake_attempts = 0
        self.total_commits = 0
        self.overspeed_events = 0
        self._server = None
        self._live: set = set()

    async def __aenter__(self):
        self._server = await serve(self._handler, '127.0.0.1', 0, process_request=self._process_request,
                                   max_size=None, logger=logging.getLogger(MOCK_LOGGER))
        self.url = f'ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}'
        self._injector = asyncio.create_task(self._inject_loop())
        return self

    async def __aexit__(self, *exc):
        self._injector.cancel()
        await asyncio.gather(self._injector, return_exceptions=True)
        self._server.close()
        await self._server.wait_closed()

    def _process_request(self, connection, request):
        attempt = self.handshake_attempts
        self.handshake_attempts += 1
        now = self.clock.now()
        status = self.handshake(attempt) if self.handshake else None
        if status is None and self.refuse_after is not None and now >= self.refuse_after:
            status = 503
        if status is None and self.refuse_between and self.refuse_between[0] <= now < self.refuse_between[1]:
            status = 503
        if status is not None:
            return connection.respond(HTTPStatus(status), 'denied\n')
        return None

    async def _inject_loop(self):
        while True:
            await asyncio.sleep(0.002)
            now = self.clock.now()
            while self.pending_drops and now >= self.pending_drops[0]:
                self.pending_drops.pop(0)
                for ws in list(self._live):
                    ws.transport.abort()
            while self.pending_1001 and now >= self.pending_1001[0]:
                self.pending_1001.pop(0)
                for ws in list(self._live):
                    asyncio.create_task(ws.close(1001, CLOSE_1001_REASON))

    async def _send(self, ws, event: dict):
        try:
            await ws.send(json.dumps(event))
        except Exception:  # noqa: BLE001 - peer gone
            pass

    def _overspeed(self, record: ConnRecord, now: float, samples: int) -> bool:
        if self.overspeed_factor is None:
            return False
        window = sum(n for t, _, n in record.appends if t > now - 2.0) + samples
        return window > self.overspeed_factor * 2.0 * SAMPLE_RATE

    async def _handler(self, ws):
        record = ConnRecord(len(self.connections), dict(ws.request.headers), ws.request.path, self.clock.now())
        self.connections.append(record)
        self._live.add(ws)
        completions: asyncio.Queue = asyncio.Queue()
        completer = asyncio.create_task(self._complete_loop(ws, completions))
        committed_end = 0
        reject_next_commit = False
        partial_count = 0
        await self._send(ws, {'type': 'session.created', 'session': {}})
        try:
            async for raw in ws:
                event = json.loads(raw)
                kind = event.get('type')
                now = self.clock.now()
                if kind == 'session.update':
                    record.session_updates.append(event)
                    await self._send(ws, {'type': 'session.updated', 'session': event.get('session', {})})
                elif kind == 'input_audio_buffer.append':
                    pcm = base64.b64decode(event['audio'])
                    samples = len(pcm) // 2
                    if self._overspeed(record, now, samples):
                        record.overspeed_drops += 1
                        self.overspeed_events += 1
                        reject_next_commit = True
                        continue
                    record.appends.append((now, marker_of(pcm), samples))
                    record.audio.extend(pcm)
                    partial_count += 1
                    if self.partial_events:
                        text = '部分' * min(partial_count, 6)
                        if self.revise_partials and partial_count % 4 == 0:
                            text = text[:1]
                        await self._send(ws, {'type': 'conversation.item.input_audio_transcription.text',
                                              'text': text, 'stash': '…'})
                elif kind == 'input_audio_buffer.commit':
                    end = len(record.audio) // 2
                    if reject_next_commit:
                        reject_next_commit = False
                        error = {'type': 'error', 'error': {'type': 'invalid_request_error', 'code': 'invalid_value',
                                                            'message': 'Error committing input audio buffer'}}
                        record.errors_sent.append('invalid_request_error')
                        await self._send(ws, error)
                        continue
                    self.total_commits += 1
                    number = self.total_commits
                    record.commits.append((committed_end, end))
                    injected = self.error_on_commit.pop(number, None)
                    if injected == 'InternalError':
                        record.errors_sent.append('InternalError')
                        await self._send(ws, {'type': 'error', 'error': {'type': 'server_error', 'code': 'InternalError',
                                                                         'message': 'Internal server error'}})
                    elif injected == 'invalid_request_error':
                        record.errors_sent.append('invalid_request_error')
                        await self._send(ws, {'type': 'error', 'error': {'type': 'invalid_request_error',
                                                                         'code': 'invalid_value',
                                                                         'message': 'Error committing input audio buffer'}})
                    elif number in self.drop_after_commit:
                        # "Committed, no result yet": this commit never completes on
                        # this connection, whatever the scheduling jitter.
                        delay = self.drop_after_commit.pop(number)
                        asyncio.create_task(self._abort_later(ws, delay))
                    elif not self.never_complete:
                        text = self.text_fn(bytes(record.audio), committed_end, end)
                        completions.put_nowait((now + self.complete_delay, text))
                    committed_end = end
                    partial_count = 0
                elif kind == 'session.finish':
                    await self._send(ws, {'type': 'session.finished'})
                    await ws.close()
        except Exception:  # noqa: BLE001 - aborted/closed connections end the record
            pass
        finally:
            completer.cancel()
            self._live.discard(ws)
            record.closed_at = self.clock.now()
            record.close_code = ws.close_code

    async def _abort_later(self, ws, delay: float):
        await self.clock.sleep(delay)
        ws.transport.abort()

    async def _complete_loop(self, ws, completions: asyncio.Queue):
        while True:
            due, text = await completions.get()
            await self.clock.sleep(due - self.clock.now())
            await self._send(ws, {'type': 'conversation.item.input_audio_transcription.completed',
                                  'transcript': text})
