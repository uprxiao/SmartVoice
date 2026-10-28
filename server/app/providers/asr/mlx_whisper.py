from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

from ...core.clock import loop_time
from ...services.asr_core import AsrConfig, AsrEngine, StreamingText
from .base import (
    AsrCapabilities,
    AsrFinal,
    AsrOpenOptions,
    AsrPartial,
    AsrProvider,
    AsrSegment,
    AsrSession,
    ProviderError,
)


class MlxAsrSession(AsrSession):
    def __init__(self, engine: AsrEngine, config: AsrConfig, options: AsrOpenOptions):
        self.engine = engine
        self.config = config
        self.options = options
        self.buffer = bytearray()
        self.base_sample = 0
        self.offset = 0
        self.decoded_samples = 0
        self.text = StreamingText()
        self.queue: asyncio.Queue[AsrPartial] = asyncio.Queue(maxsize=8)
        self.task: asyncio.Task | None = None
        self.cancelled = False
        self.last_partial_at = 0.0
        self.finishing = False
        # Correct words only, no aliases. The adapter also enforces the public
        # 100-term/2000-codepoint bound even when called outside SessionManager.
        hints, count = [], 0
        for term in options.hint_terms:
            cost = len(term) + bool(hints)
            if len(hints) < 100 and count + cost <= 2000:
                hints.append(term); count += cost
        self.hint_prompt = " ".join(hints)
        self.owner = (options.user_id, options.session_id, options.attempt_no)
        from ...services.inference_context import CallScope
        self.scope = CallScope(options.user_id, options.session_id, options.attempt_no) if options.user_id else None

    def _prompt(self):
        context = self.text.prompt(self.offset)
        return self.hint_prompt + ("\n" + context if self.hint_prompt and context else context)

    def _received(self) -> int:
        return self.base_sample + len(self.buffer) // 2

    async def feed(self, pcm_s16le: bytes, start_sample: int) -> None:
        if self.cancelled:
            return
        expected = self._received()
        if start_sample != expected:
            raise ProviderError("invalid_input", "audio samples are not contiguous")
        self.buffer.extend(pcm_s16le)
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while not self.cancelled and not self.finishing:
                await asyncio.sleep(max(self.config.step_seconds, 0.25))
                if self.cancelled or self.finishing:
                    return
                await self._decode_window()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.queue.full():
                self.queue.get_nowait()
            self.queue.put_nowait(exc)  # type: ignore[arg-type]

    async def _decode_window(self) -> None:
        end = self._received()
        if end - self.offset < int(self.config.step_seconds * 16000):
            return
        def latest_window():
            nonlocal end
            end = self._received()
            audio = bytes(self.buffer[(self.offset - self.base_sample) * 2 : (end - self.base_sample) * 2])
            return audio, self._prompt()
        try:
            limits = {}
            if self.options.eligibility_deadline is not None:
                original_deadline = loop_time() + self.engine.decode_timeout_ms / 1000
                if self.options.outcome_deadline is not None:
                    original_deadline = min(original_deadline, self.options.outcome_deadline)
                limits = {"deadline": min(original_deadline, self.options.eligibility_deadline),
                          "outcome_deadline": original_deadline}
            result = await self.engine.decode(latest_window, "", owner=self.owner, scope=self.scope, **limits)
        except Exception as exc:
            raise ProviderError("resource_exhausted", str(exc), retryable=True) from exc
        if self.cancelled:
            return
        self.text.update(result, self.offset)
        self.decoded_samples = end
        new_offset = self.text.trim_point(result, self.offset, end, self.config.trim_seconds)
        if new_offset > self.base_sample:
            del self.buffer[: (new_offset - self.base_sample) * 2]
            self.base_sample = new_offset
        self.offset = new_offset
        now = time.monotonic()
        if now - self.last_partial_at >= 0.2:
            self.last_partial_at = now
            partial = AsrPartial(
                revision=self.text.revision,
                stable_text=self.text.stable,
                provisional_text=self.text.provisional,
                decoded_samples=end,
                received_samples=end,
            )
            if self.queue.full():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self.queue.put_nowait(partial)

    async def partials(self) -> AsyncIterator[AsrPartial]:
        while True:
            item = await self.queue.get()
            if isinstance(item, Exception):
                raise item
            yield item
            if self.cancelled:
                return

    async def finish(self, total_samples: int, deadline: float) -> AsrFinal:
        if self.cancelled:
            raise ProviderError("cancelled", "session cancelled")
        self.finishing = True
        # Finish waits for the one in-flight window; it does not launch an
        # overlapping process invocation for the same owner.
        await self.engine.cancel(self.owner, queued_only=True)
        if self.task:
            try:
                async with asyncio.timeout_at(deadline):
                    await asyncio.shield(asyncio.gather(self.task, return_exceptions=True))
            except TimeoutError:
                await self.cancel()
                raise ProviderError("timeout", "ASR finish deadline elapsed")
        end = self._received()
        if end != total_samples:
            raise ProviderError("invalid_input", "finish sample count differs from received audio")
        if end > self.offset:
            # A queued finish owns a provider reference, not an immutable PCM
            # copy that could survive cancellation/ledger cleanup in this frame.
            def final_window():
                if self.cancelled or loop_time() >= deadline:
                    raise TimeoutError("ASR input eligibility elapsed")
                return (bytes(self.buffer[(self.offset - self.base_sample) * 2 :]), self._prompt())
            try:
                result = await asyncio.wait_for(
                    self.engine.decode(final_window, self._prompt(),
                        owner=self.owner, scope=self.scope, purpose="asr_finish", deadline=deadline,
                        outcome_deadline=self.options.outcome_deadline),
                    timeout=max(0, min(10.0, deadline - loop_time())),
                )
            except asyncio.TimeoutError as exc:
                raise ProviderError("timeout", "ASR finish timed out", retryable=True) from exc
            self.text.update(result, self.offset)
        final_text = self.text.text
        self.buffer.clear()
        segments = [AsrSegment(0, max(total_samples, end), final_text)] if final_text else []
        return AsrFinal(
            text=final_text,
            segments=segments,
            no_speech=not final_text,
            model_id=self.engine.model_id,
            processed_samples=max(total_samples, end),
        )

    async def cancel(self) -> None:
        self.cancelled = True
        self.hint_prompt = ""
        self.buffer.clear()  # Never wait for a ledger/DB before releasing local PCM.
        self.engine.release_audio(self.owner)
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        await self.engine.cancel(self.owner)
        self.buffer.clear()


class MlxWhisperProvider(AsrProvider):
    capabilities = AsrCapabilities(backend_version="mlx-whisper")

    def __init__(self, config: AsrConfig, model_id: str, **engine_options):
        self.capabilities = AsrCapabilities(backend_version="mlx-whisper")
        self.config = config
        self.model_id = model_id
        self.engine = AsrEngine(config, **engine_options)

    @property
    def scheduler(self):
        return self.engine.scheduler

    @property
    def recorder(self):
        return self.engine.recorder

    @recorder.setter
    def recorder(self, value):
        self.engine.recorder = value

    async def prepare(self) -> None:
        try:
            await self.engine.prepare()
        except Exception as exc:
            raise ProviderError("unavailable", str(exc)) from exc

    async def open(self, options: AsrOpenOptions) -> AsrSession:
        return MlxAsrSession(self.engine, self.config, options)

    async def health(self) -> bool:
        for slot, repair in list(self.engine.repairs.items()):
            if repair.done() and not repair.cancelled() and repair.exception() is None:
                self.engine.repairs.pop(slot, None)
        return bool(self.engine.model_id) and all(
            not getattr(worker, "stop_incomplete", False)
            and worker.process is not None and worker.process.is_alive()
            and slot not in self.engine.repairs
            for slot, worker in enumerate(self.engine.workers))

    async def close(self) -> None:
        await self.engine.close()
