"""Shared helpers for cloud ASR provider tests (fictional synthetic audio only).

``ScaledClock`` runs audio time faster than wall time; the provider and the
mock cloud share one instance so pacing and overspeed checks stay consistent.
Every 250 ms block starts with its absolute block index as a marker sample.
"""
from __future__ import annotations

import asyncio
import time

import numpy as np

from app.core.egress import EgressPolicy
from app.providers.asr.bailian_realtime import BailianRealtimeConfig, BailianRealtimeProvider, TimeSource
from app.providers.asr.base import AsrOpenOptions

SAMPLE_RATE = 16_000
BLOCK = 4_000
TEST_KEY = 'sk-test-7c1e9a5b2d4f'


class ScaledClock(TimeSource):
    def __init__(self, speed: float):
        self.speed = speed
        self.origin = time.monotonic()

    def now(self) -> float:
        return (time.monotonic() - self.origin) * self.speed

    def reset(self) -> None:
        """Make "now" audio second 0 (e.g. when the user starts speaking)."""
        self.origin = time.monotonic()

    def wait_until(self, instant: float) -> None:
        """Blocking wait for threads outside the event loop (TestClient side)."""
        delay = (instant - self.now()) / self.speed
        if delay > 0:
            time.sleep(delay)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds) / self.speed)


def synth(duration_s: float, silences: tuple[tuple[float, float], ...] = (), *, amplitude: int = 3000,
          seed: int = 7) -> bytes:
    """Tone with exact silent spans (F-V2-TONE / F-V2-FLOW) and block markers."""
    count = int(duration_s * SAMPLE_RATE)
    t = np.arange(count) / SAMPLE_RATE
    rng = np.random.default_rng(seed)
    signal = amplitude * np.sin(2 * np.pi * 220 * t) + rng.normal(0, amplitude * 0.05, count)
    for start, length in silences:
        signal[int(start * SAMPLE_RATE):int((start + length) * SAMPLE_RATE)] = 0
    pcm = np.clip(signal, -32768, 32767).astype('<i2')
    pcm[::BLOCK] = np.arange(len(pcm[::BLOCK]), dtype='<i2')
    return pcm.tobytes()


def silence(duration_s: float) -> bytes:
    pcm = np.zeros(int(duration_s * SAMPLE_RATE), dtype='<i2')
    pcm[::BLOCK] = np.arange(len(pcm[::BLOCK]), dtype='<i2')
    return pcm.tobytes()


def expected_text(start: int, end: int) -> str:
    return f'seg[{start // BLOCK}-{(end - 1) // BLOCK}]'


class AllowAll(EgressPolicy):
    """Loopback mock endpoints are local addresses; real policy, local mode."""

    def __init__(self):
        super().__init__('deny-public')


def make_provider(server, clock, **overrides) -> BailianRealtimeProvider:
    values = dict(base_url=server.url, api_key=TEST_KEY, language='zh', connect_timeout_ms=5_000)
    values.update(overrides)
    return BailianRealtimeProvider(BailianRealtimeConfig(**values), egress=AllowAll(), clock=clock)


def options(max_seconds: int = 1_500, **kw) -> AsrOpenOptions:
    return AsrOpenOptions(session_id=kw.pop('session_id', 'sess-v2-test'), attempt_no=kw.pop('attempt_no', 1),
                          max_samples=max_seconds * SAMPLE_RATE, user_id='user-v2-test', **kw)


async def feed_realtime(session, pcm: bytes, clock, *, start_sample: int = 0, until_s: float | None = None,
                        fed: dict | None = None):
    """Feed 250 ms blocks at their real-time arrival instants (end of block).

    ``fed`` (optional) receives ``block index → clock time actually fed``."""
    t0 = clock.now() - start_sample / SAMPLE_RATE
    total = len(pcm) // 2
    stop = total if until_s is None else min(total, int(until_s * SAMPLE_RATE))
    offset = start_sample
    while offset < stop:
        end = min(offset + BLOCK, stop)
        await clock.sleep(t0 + end / SAMPLE_RATE - clock.now())
        await session.feed(pcm[offset * 2:end * 2], offset)
        if fed is not None:
            fed[offset // BLOCK] = clock.now()
        offset = end
    return offset


def loop_deadline(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds
