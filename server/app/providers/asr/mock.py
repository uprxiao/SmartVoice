from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from .base import (
    AsrCapabilities,
    AsrFinal,
    AsrOpenOptions,
    AsrPartial,
    AsrProvider,
    AsrSegment,
    AsrSession,
)


class MockAsrSession(AsrSession):
    def __init__(self, options: AsrOpenOptions, text: str):
        self.options = options
        self.text = text
        self.samples = 0
        self.revision = 0
        self.cancelled = False

    async def feed(self, pcm_s16le: bytes, start_sample: int) -> None:
        if self.cancelled:
            return
        self.samples = max(self.samples, start_sample + len(pcm_s16le) // 2)

    async def partials(self) -> AsyncIterator[AsrPartial]:
        # Mock provider intentionally emits no partials until finish. Tests can
        # subclass this to simulate stable partial events.
        if False:
            yield  # pragma: no cover

    async def finish(self, total_samples: int, deadline: float) -> AsrFinal:
        if self.cancelled:
            raise asyncio.CancelledError()
        text = self.text if total_samples > 0 else ""
        return AsrFinal(
            text=text,
            segments=[AsrSegment(0, total_samples, text)] if text else [],
            no_speech=not text,
            model_id="mock-asr",
            processed_samples=total_samples,
        )

    async def cancel(self) -> None:
        self.cancelled = True


class MockAsrProvider(AsrProvider):
    model_id = "mock-asr"
    capabilities = AsrCapabilities(backend_version="mock-1")

    def __init__(self, text: str = "嗯那个，帮我加一个按钮，不要修改数据库。"):
        self.text = text

    async def prepare(self) -> None:
        return None

    async def open(self, options: AsrOpenOptions) -> AsrSession:
        return MockAsrSession(options, self.text)

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None
