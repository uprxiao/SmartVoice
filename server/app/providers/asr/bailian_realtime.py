"""Bailian ``qwen3-asr-flash-realtime`` cloud ASR provider (V2 design §3).

One ``BailianSession`` owns: a causal ``Segmenter``; a segment ledger
(``open → committed → completed``) that keeps PCM only for segments without a
result; a single paced sender (on-time blocks go out on arrival, late blocks
at ``catchup_rate`` × real time, commits in order); and a recovery loop that
reconnects and resends from the first segment without a result.

``stable_text`` only grows when a segment completes; the in-flight segment's
text is always ``provisional_text`` so a reconnect can never regress the
committed prefix the runtime validates.
"""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import math
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from websockets.exceptions import ConnectionClosed

from ...core.clock import loop_time
from ...core.egress import EgressDenied, EgressPolicy
from ...services.inference_context import OUTCOME_WRITE_BUDGET_SECONDS, CallScope, persist_outcome
from . import bailian_conn
from .bailian_conn import COMMIT_MESSAGE, FINISH_MESSAGE, RECOVERABLE, CloudError
from .bailian_segmenter import BLOCK_SAMPLES, SAMPLE_RATE, Segmenter
from .base import (
    AsrCapabilities,
    AsrFinal,
    AsrOpenOptions,
    AsrPartial,
    AsrResume,
    AsrSegment,
    ProviderError,
)

log = logging.getLogger('svi.asr.bailian')
PROVIDER_TYPE = 'bailian_realtime'
RETRYABLE = frozenset({'unavailable', 'rate_limited', 'timeout', 'busy'})
# The cloud model may return a lone filler ("嗯。") for pure silence. Such text is
# dropped only for segments without a single voiced block (design §15 C14).
_FILLER = re.compile(r'^(?:嗯|啊|呃|额|哦|唔|噢|um|uh|en|hmm)*$', re.IGNORECASE)
_PUNCT = re.compile(r'[\s\W_]+', re.UNICODE)
CLOSE_CONFIRM_S = 2.0
READY_WAIT_S = 1.0


def reconnect_delay(attempt: int) -> float:
    """Exponential backoff 0.25, 0.5, 1, 2, 4, 4 … s: the default 8 attempts span
    ~20 s, so a short cloud/egress outage does not exhaust the budget (S5 E2E-04
    finding: linear 0.1 s steps used all 8 attempts within a 3 s outage)."""
    return min(0.25 * 2 ** (attempt - 1), 4.0)


class TimeSource:
    """Pacing clock; tests substitute a scaled clock shared with the mock cloud."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


@dataclass(frozen=True)
class BailianRealtimeConfig:
    """Already-validated settings (parameter allowlist lives in config, §6.2)."""
    base_url: str
    api_key: str
    model_id: str = 'qwen3-asr-flash-realtime'
    language: str = 'auto'
    segment_target_s: float = 30
    segment_window_s: float = 5
    segment_silence_ratio: float = 0.15
    segment_min_s: float = 10
    catchup_rate: float = 2.0
    max_reconnects: int = 8
    max_record_seconds: int = 360
    max_concurrent_sessions: int = 8
    use_env_proxy: bool = False
    connect_timeout_ms: int = 15_000
    provider_id: str = PROVIDER_TYPE
    provider_version: str = '1'
    provider_config_id: str | None = None  # admin-managed row id; None for YAML


class _Segment:
    __slots__ = ('start', 'end', 'text', 'voiced')

    def __init__(self, start: int, end: int | None = None, text: str | None = None, voiced: bool | None = None):
        self.start, self.end, self.text, self.voiced = start, end, text, voiced


def _silence_filler(text: str) -> bool:
    return bool(_FILLER.match(_PUNCT.sub('', text)))


class _Connection:
    def __init__(self, ws, first_segment: int, cursor: int):
        self.ws = ws
        self.first_segment = first_segment
        self.cursor = cursor
        self.commits = 0
        self.completed = 0
        self.ready = asyncio.Event()


class BailianSession:
    def __init__(self, provider: BailianRealtimeProvider, options: AsrOpenOptions):
        self.provider = provider
        self.config = provider.config
        self.clock = provider.clock
        self.options = options
        self.segments: list[_Segment] = []
        start = 0
        if options.resume is not None:
            for seg in options.resume.segments:
                self.segments.append(_Segment(seg.start_sample, seg.end_sample, seg.text))
            start = options.resume.end_sample
        self.segments.append(_Segment(start))
        self.origin = self.received = self.pcm_base = self.sent_high = start
        self.pcm = bytearray()
        self.arrival_ends: list[int] = []
        self.arrival_times: list[float] = []
        self.segmenter = Segmenter(
            target_s=self.config.segment_target_s, window_s=self.config.segment_window_s,
            min_s=self.config.segment_min_s, silence_ratio=self.config.segment_silence_ratio,
            start_sample=start)
        self.provisional = ''
        self.revision = 0
        self.reconnects = 0
        self.resend_samples = 0
        self.connection: _Connection | None = None
        self.failure: ProviderError | None = None
        self.finishing = self.done = self.cancelled = self.closed = False
        self.wake = asyncio.Event()      # sender: new audio, cut or finish
        self.changed = asyncio.Event()   # partials: text/decoded change
        self.settled = asyncio.Event()   # finish: all completed or failed
        self.task: asyncio.Task | None = None
        self._pre_drop_len: dict[int, int] = {}
        self.call_id = ''
        self.recorded = False
        self.started_at = loop_time()
        if options.resume is not None and options.resume.segments:
            self.changed.set()

    # ----- ledger -------------------------------------------------------
    def _first_incomplete(self) -> int:
        for index, seg in enumerate(self.segments):
            if seg.text is None:
                return index
        return len(self.segments)

    def _all_completed(self) -> bool:
        return self.finishing and self._first_incomplete() == len(self.segments)

    def _stable_text(self) -> str:
        return ''.join(seg.text for seg in self.segments[:self._first_incomplete()])

    def _decoded(self) -> int:
        index = self._first_incomplete()
        return self.segments[index - 1].end if index else self.origin

    def _completed_segments(self) -> tuple[AsrSegment, ...]:
        return tuple(AsrSegment(seg.start, seg.end, seg.text)
                     for seg in self.segments[:self._first_incomplete()])

    def _resume(self) -> AsrResume:
        return AsrResume(self.config.provider_id, self.config.provider_version, self._completed_segments())

    def _trim(self) -> None:
        index = self._first_incomplete()
        base = self.segments[index].start if index < len(self.segments) else self.received
        if base > self.pcm_base:
            del self.pcm[:(base - self.pcm_base) * 2]
            self.pcm_base = base
            keep = bisect.bisect_right(self.arrival_ends, base)
            del self.arrival_ends[:keep]
            del self.arrival_times[:keep]

    def _arrival(self, end_sample: int) -> float:
        index = bisect.bisect_left(self.arrival_ends, end_sample)
        if index >= len(self.arrival_times):
            return self.clock.now()
        return self.arrival_times[index]

    def _aligned(self, sample: int) -> int:
        return self.origin + (sample - self.origin) // BLOCK_SAMPLES * BLOCK_SAMPLES

    def _notify(self) -> None:
        self.changed.set()

    # ----- AsrSession ---------------------------------------------------
    def start(self) -> None:
        # Connect before the first block arrives (design §3.3 open).
        self.task = asyncio.create_task(self._run())

    async def feed(self, pcm_s16le: bytes, start_sample: int) -> None:
        if self.cancelled:
            raise ProviderError('cancelled', 'cloud ASR session cancelled')
        if self.finishing or self.done or len(pcm_s16le) % 2 or start_sample != self.received:
            raise ProviderError('invalid_input', 'cloud ASR samples are not contiguous PCM')
        samples = len(pcm_s16le) // 2
        if self.received + samples > self.options.max_samples:
            raise ProviderError('invalid_input', 'cloud ASR audio exceeds session limit')
        if self.failure is not None:
            # The cloud stream already failed. Keep accepting the upload so the
            # runtime holds complete audio for an explicit retry (BR-05); the
            # failure is reported once, by finish.
            self.received += samples
            return
        self.pcm.extend(pcm_s16le)
        self.received += samples
        self.arrival_ends.append(self.received)
        self.arrival_times.append(self.clock.now())
        for cut in self.segmenter.push(pcm_s16le):
            self.segments[-1].end = cut
            self.segments[-1].voiced = self.segmenter.voiced_by_cut.pop(cut, True)
            self.segments.append(_Segment(cut))
        self.wake.set()

    async def partials(self) -> AsyncIterator[AsrPartial]:
        last = None
        while True:
            await self.changed.wait()
            self.changed.clear()
            if self.cancelled or self.failure is not None:
                return
            snapshot = (self._stable_text(), self.provisional, self._decoded())
            if snapshot != last:
                last = snapshot
                self.revision += 1
                yield AsrPartial(self.revision, snapshot[0], snapshot[1], snapshot[2], self.received)
            if self.done:
                return

    def drain_estimate_s(self) -> float:
        """Seconds still needed to send backlog at the catch-up rate."""
        if self.connection is not None:
            sent = self.connection.cursor
        else:
            index = self._first_incomplete()
            sent = self.segments[index].start if index < len(self.segments) else self.received
        backlog = max(0, self.received - sent) / SAMPLE_RATE
        rate = self.config.catchup_rate
        return 0.0 if math.isinf(rate) else backlog / rate

    def metrics(self) -> dict:
        return {'segments': self._first_incomplete(), 'reconnects': self.reconnects,
                'resend_s': round(self.resend_samples / SAMPLE_RATE, 3)}

    async def finish(self, total_samples: int, deadline: float) -> AsrFinal:
        if self.cancelled:
            raise ProviderError('cancelled', 'cloud ASR session cancelled')
        if self.failure is not None:
            await self._shutdown()
            raise self.failure
        if self.finishing or self.done or total_samples != self.received:
            raise ProviderError('invalid_input', 'cloud ASR finish sample count mismatch')
        self.finishing = True
        # Reserve the model-call outcome write inside the caller's budget.
        wait_deadline = deadline - (OUTCOME_WRITE_BUDGET_SECONDS if self.call_id else 0.0)
        tail = self.segments[-1]
        if tail.end is None:
            if self.received > tail.start:
                tail.end = self.received
                tail.voiced = self.segmenter.tail_voiced()
            else:
                self.segments.pop()
        if self._all_completed():
            self.settled.set()
        self.wake.set()
        try:
            async with asyncio.timeout_at(wait_deadline):
                await self.settled.wait()
        except TimeoutError:
            error = ProviderError('timeout', 'cloud ASR finish deadline elapsed', retryable=True,
                                  resume=self._resume())
            if self.failure is None:
                self.failure = error
            log.warning('bailian finish timeout session=%s segments=%d reconnects=%d',
                        self.options.session_id, self._first_incomplete(), self.reconnects)
            await self._shutdown()
            raise error from None
        if self.failure is not None:
            await self._shutdown()
            raise self.failure
        texts = [seg.text for seg in self.segments]
        final = AsrFinal(
            text=''.join(texts),
            segments=[AsrSegment(seg.start, seg.end, seg.text) for seg in self.segments],
            no_speech=not any(text.strip() for text in texts),
            model_id=self.provider.model_id,
            processed_samples=total_samples,
        )
        recorded = await self._record('succeeded', None, deadline)
        self.done = True
        self._notify()
        await self._shutdown()
        if not recorded:
            raise ProviderError('ledger_unavailable', 'cloud ASR model outcome was not persisted')
        return final

    async def _record(self, status: str, error_code: str | None, deadline: float | None = None) -> bool:
        if self.recorded or not self.call_id:
            return True
        self.recorded = True
        outcome = dict(call_id=self.call_id, status=status, error_code=error_code,
                       elapsed_ms=int((loop_time() - self.started_at) * 1000),
                       processed_audio_samples=self.received,
                       segments=self._first_incomplete(), reconnects=self.reconnects)
        limit = deadline if deadline is not None else loop_time() + OUTCOME_WRITE_BUDGET_SECONDS
        return await persist_outcome(self.provider.recorder, outcome, limit)

    async def cancel(self) -> None:
        if not self.cancelled:
            self.cancelled = True
            self.pcm.clear()
            self.arrival_ends.clear()
            self.arrival_times.clear()
            for seg in self.segments:
                seg.text = None
            self.provisional = ''
            self.settled.set()
            self.changed.set()
            self.wake.set()
        await self._shutdown()

    # ----- lifecycle ----------------------------------------------------
    def _fail(self, code: str) -> None:
        if self.failure is not None or self.done or self.cancelled:
            return
        self.failure = ProviderError(code, f'cloud ASR {code}', retryable=code in RETRYABLE,
                                     resume=self._resume())
        if code == 'bad_request':
            # Usually our own protocol violation (e.g. pacing); must alert.
            log.warning('bailian protocol rejected (alert) session=%s segments=%d',
                        self.options.session_id, self._first_incomplete())
        log.warning('bailian session failed session=%s code=%s segments=%d reconnects=%d',
                    self.options.session_id, code, self._first_incomplete(), self.reconnects)
        self.settled.set()
        self.changed.set()
        self.wake.set()

    async def _shutdown(self) -> None:
        if not self.done and not self.recorded:
            if self.cancelled:
                await self._record('cancelled', 'CANCELLED')
            elif self.failure is not None:
                await self._record('failed', self.failure.code)
        task = self.task
        if task is not None and not task.done():
            if not self.done:
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), CLOSE_CONFIRM_S)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.pcm.clear()
        self.arrival_ends.clear()
        self.arrival_times.clear()
        self.closed = True
        self.provider._release(self)

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self._connection_round()
                    return
                except CloudError as exc:
                    if self.cancelled or self.done:
                        return
                    if exc.code not in RECOVERABLE:
                        self._fail(exc.code)
                        return
                    if self.reconnects >= self.config.max_reconnects:
                        self._fail('unavailable')
                        return
                    self.reconnects += 1
                    log.info('bailian reconnect session=%s attempt=%d reason=%s first_segment=%d',
                             self.options.session_id, self.reconnects, exc.detail or exc.code,
                             self._first_incomplete())
                    await self.clock.sleep(reconnect_delay(self.reconnects))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak an unmapped error
            log.warning('bailian unexpected error session=%s type=%s',
                        self.options.session_id, type(exc).__name__)
            self._fail('unavailable')
        finally:
            if self.failure is not None:
                self.pcm.clear()
                self.provider._release(self)

    async def _connection_round(self) -> None:
        settings = bailian_conn.ConnectSettings(
            base_url=self.config.base_url, model_id=self.config.model_id, api_key=self.config.api_key,
            connect_timeout_s=self.config.connect_timeout_ms / 1000, use_env_proxy=self.config.use_env_proxy)
        ws = await bailian_conn.connect(settings, self.provider.egress)
        first = self._first_incomplete()
        start = self.segments[first].start if first < len(self.segments) else self.received
        connection = self.connection = _Connection(ws, first, start)
        reader = sender = None
        try:
            await ws.send(bailian_conn.session_update(self.config.language))
            reader = asyncio.create_task(self._reader(connection))
            sender = asyncio.create_task(self._sender(connection))
            waiting = {reader, sender}
            while not reader.done():
                done, waiting = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except ConnectionClosed as exc:
            if not self.done:
                self._pre_drop_len[self._first_incomplete()] = len(self.provisional)
            code = exc.rcvd.code if exc.rcvd is not None else 'none'
            raise CloudError('unavailable', f'closed_{code}') from exc
        except OSError as exc:
            raise CloudError('unavailable', type(exc).__name__) from exc
        except CloudError as exc:
            if exc.code in RECOVERABLE:
                self._pre_drop_len[self._first_incomplete()] = len(self.provisional)
            raise
        finally:
            self.connection = None
            for task in (reader, sender):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (reader, sender) if t is not None), return_exceptions=True)
            await _close(ws)

    async def _reader(self, connection: _Connection) -> None:
        async for raw in connection.ws:
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            kind = str(event.get('type') or '')
            if kind == 'session.updated':
                connection.ready.set()
            elif kind.endswith('input_audio_transcription.text'):
                self.provisional = str(event.get('text') or '') + str(event.get('stash') or '')
                self._notify()
            elif kind.endswith('input_audio_transcription.completed'):
                index = connection.first_segment + connection.completed
                if connection.completed >= connection.commits or index >= len(self.segments):
                    raise CloudError('bad_request', 'unexpected_completed')
                text = event.get('transcript')
                text = str(text if text is not None else event.get('text') or '')
                if self.segments[index].voiced is False and _silence_filler(text):
                    text = ''
                self.segments[index].text = text
                connection.completed += 1
                self.provisional = ''
                if index in self._pre_drop_len:
                    # Recovery diagnostics: lengths and segment numbers only (§3.8).
                    log.info('bailian recovered segment session=%s segment=%d before_len=%d after_len=%d',
                             self.options.session_id, index, self._pre_drop_len.pop(index),
                             len(self.segments[index].text))
                self._trim()
                self._notify()
                if self._all_completed():
                    self.settled.set()
                    try:
                        await connection.ws.send(FINISH_MESSAGE)
                    except ConnectionClosed:
                        pass
                    return
            elif kind == 'error':
                raise bailian_conn.map_error_event(event)
        if not (self.done or self._all_completed()):
            raise CloudError('unavailable', f'closed_{connection.ws.close_code}')

    async def _sender(self, connection: _Connection) -> None:
        try:
            await asyncio.wait_for(connection.ready.wait(), READY_WAIT_S)
        except TimeoutError:
            pass
        rate = self.config.catchup_rate
        index = connection.first_segment
        last_send: float | None = None
        while not self.cancelled:
            if index >= len(self.segments):
                return
            seg = self.segments[index]
            if seg.end is not None and connection.cursor >= seg.end:
                await connection.ws.send(COMMIT_MESSAGE)
                connection.commits += 1
                index += 1
                continue
            limit = self.received if self.finishing else self._aligned(self.received)
            if seg.end is not None:
                limit = min(limit, seg.end)
            if connection.cursor >= limit:
                self.wake.clear()
                await self.wake.wait()
                continue
            cursor = connection.cursor
            end = min(cursor + BLOCK_SAMPLES, limit)
            arrival = self._arrival(end)
            gap = 0.0 if math.isinf(rate) else (end - cursor) / SAMPLE_RATE / rate
            send_at = arrival if last_send is None else max(arrival, last_send + gap)
            delay = send_at - self.clock.now()
            if delay > 0:
                await self.clock.sleep(delay)
            if self.cancelled:
                return
            chunk = bytes(self.pcm[(cursor - self.pcm_base) * 2:(end - self.pcm_base) * 2])
            await connection.ws.send(bailian_conn.append_message(chunk))
            if cursor < self.sent_high:
                self.resend_samples += min(end, self.sent_high) - cursor
            self.sent_high = max(self.sent_high, end)
            connection.cursor = end
            last_send = self.clock.now()


async def _close(ws) -> None:
    """Close handshake bounded to half the confirm budget, then always abort."""
    try:
        await asyncio.wait_for(ws.close(), CLOSE_CONFIRM_S / 2)
    except Exception:  # noqa: BLE001 - fall through to a hard abort
        pass
    finally:
        transport = getattr(ws, 'transport', None)
        if transport is not None:
            transport.abort()


class BailianRealtimeProvider:
    def __init__(self, config: BailianRealtimeConfig, *, egress: EgressPolicy,
                 clock: TimeSource | None = None):
        self.config = config
        self.model_id = config.model_id
        self.egress = egress
        self.clock = clock or TimeSource()
        self.capabilities = AsrCapabilities(
            backend_version=f'bailian/{config.model_id}', provider_id=config.provider_id,
            provider_version=config.provider_version,
            supports_hints=False, replay_rate=config.catchup_rate,
            max_record_seconds=config.max_record_seconds,
            max_concurrent_sessions=config.max_concurrent_sessions, supports_resume=True)
        self.sessions: set[BailianSession] = set()
        self.closing = False
        self.recorder = None  # DatabaseCallRecorder, attached by the app

    @property
    def active_sessions(self) -> int:
        return len(self.sessions)

    async def prepare(self) -> None:
        try:
            self.egress.check_url(self.config.base_url, kind='asr')
        except EgressDenied as exc:
            raise ProviderError('egress_denied', 'cloud ASR address rejected by egress policy') from exc

    async def open(self, options: AsrOpenOptions) -> BailianSession:
        if self.closing:
            raise ProviderError('unavailable', 'cloud ASR provider is closing', retryable=True)
        resume = options.resume
        if resume is not None and (resume.provider_id, resume.provider_version) != (
                self.config.provider_id, self.config.provider_version):
            raise ProviderError('invalid_input', 'resume data belongs to another provider')
        if len(self.sessions) >= self.config.max_concurrent_sessions:
            raise ProviderError('busy', 'cloud ASR provider concurrency limit reached', retryable=True)
        session = BailianSession(self, options)
        self.sessions.add(session)
        if self.recorder is not None and options.user_id:
            try:
                extra = ({'provider_config_id': self.config.provider_config_id}
                         if self.config.provider_config_id else {})
                session.call_id = await self.recorder.started(
                    CallScope(options.user_id, options.session_id, options.attempt_no),
                    PROVIDER_TYPE, self.model_id, 'asr_finish', **extra)
            except Exception as exc:
                self.sessions.discard(session)
                raise ProviderError('ledger_unavailable', 'cloud ASR model call could not be recorded') from exc
        session.start()
        return session

    def _release(self, session: BailianSession) -> None:
        self.sessions.discard(session)

    async def health(self) -> bool:
        return not self.closing

    async def close(self) -> None:
        self.closing = True
        await asyncio.gather(*(s.cancel() for s in list(self.sessions)), return_exceptions=True)
