from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..core.clock import loop_time
from ..vendor.hypothesis import HypothesisBuffer


@dataclass
class AsrConfig:
    """Local ASR config compatible with the legacy MLX engine."""

    model_dir: Path
    language: str = "auto"
    task: str = "transcribe"
    incremental: bool = True
    streaming_adapter: str = "local_agreement"
    prewarm: bool = True
    step_seconds: float = 1.0
    trim_seconds: float = 12.0


def transcribe(pcm: bytes, model_dir: str, language: str, prompt: str):
    # Offline is enforced before importing the model loader in the isolated worker.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    import numpy as np
    import mlx_whisper

    audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
    started = time.monotonic()
    if len(audio) == 0:
        return {'words': [], 'segments': [], 'seconds': 0}
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=model_dir, language=None if language == 'auto' else language,
        task='transcribe', word_timestamps=True, initial_prompt=prompt or None,
        condition_on_previous_text=True, temperature=0, verbose=False)
    # Amplitude alone does not establish absence of speech. Preserve the full
    # input and keep the model's no_speech policy for every nonzero clip.
    # Exact digital silence is filtered after inference so warm-up and physical
    # invocation accounting do not report an inference that was skipped.
    silent = not bool(np.any(audio))
    segments = [] if silent else [s for s in result.get('segments', []) if s.get('no_speech_prob', 0) < 0.8]
    return {'words': [(w['start'], w['end'], w['word']) for s in segments for w in s.get('words', [])],
            'segments': [s['end'] for s in segments], 'seconds': time.monotonic() - started}


def _worker_main(connection):
    """One persistent model process per configured execution slot."""
    try:
        progress_lock_ready = False
        while True:
            message = connection.recv()
            if message is None:
                return
            try:
                if not progress_lock_ready:
                    # This worker handles one model call at a time. tqdm's
                    # default multiprocessing RLock survives a forced worker
                    # exit until the server parent exits; a process-local
                    # thread lock keeps the same logging synchronization
                    # without creating that unrelated named semaphore.
                    from threading import RLock
                    from tqdm import tqdm
                    tqdm.set_lock(RLock())
                    progress_lock_ready = True
                value = transcribe(*message)
                connection.send((True, value))
            except BaseException as exc:
                connection.send((False, type(exc).__name__))
            finally:
                message = None
                value = None
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class DecodeWorker:
    def __init__(self, target=None):
        self.target = target
        self.process = None
        self.connection = None
        self._stop_task: asyncio.Task[None] | None = None

    @property
    def stop_incomplete(self):
        task = self._stop_task
        return task is not None and (
            not task.done() or task.cancelled() or task.exception() is not None)

    def _require_previous_stop_complete(self):
        task = self._stop_task
        if task is None:
            return
        if not task.done():
            raise RuntimeError("ASR worker stop is still in progress")
        if task.cancelled():
            raise RuntimeError("ASR worker stop was not confirmed")
        # A failed stop quarantines this worker. Never start a new process on
        # the strength of a cleared reference or a cancelled cleanup waiter.
        task.result()

    def _start(self):
        self._require_previous_stop_complete()
        if self.process is not None or self.connection is not None:
            raise RuntimeError("ASR worker still owns a process or connection")
        # Only a successfully completed stop can yield to a new generation.
        self._stop_task = None
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        self.process = ctx.Process(target=self.target or _worker_main, args=(child,), daemon=True)
        self.process.start()
        child.close()
        self.connection = parent

    async def _stop_owned(self, process, connection):
        # Keep the original handles visible until physical exit and handle
        # closure are confirmed. Failed/cancelled inference has no result left
        # to publish: escalate promptly so cleanup leaves the original outcome
        # budget usable, while still confirming exit after SIGKILL.
        if process is not None:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 0.1)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 1.0)
            if process.is_alive():
                raise RuntimeError("ASR worker could not be stopped")
            process.close()
        if connection is not None:
            connection.close()
        if self.process is process and self.connection is connection:
            self.process = self.connection = None

    async def stop(self):
        # No await between selecting the exact generation and publishing its
        # sole owner. Concurrent stop/close/decode callers join this same task.
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(
                self._stop_owned(self.process, self.connection),
                name="svi-asr-worker-stop")
        owner = self._stop_task
        interrupted = None
        while not owner.done():
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError as exc:
                # A second (or later) caller cancellation must not release the
                # execution slot while the original OS process is still alive.
                interrupted = exc
        if owner.cancelled():
            raise RuntimeError("ASR worker stop was not confirmed")
        owner.result()  # Preserve failure; do not clear the quarantined owner.
        if interrupted is not None:
            raise interrupted

    @staticmethod
    def _send_before_deadline(connection, args, deadline):
        # Executor scheduling is another dispatch boundary. The timestamp is
        # conservatively mapped from the original loop deadline, not reset.
        if time.monotonic() >= deadline or (isinstance(args[0], bytearray) and not args[0]):
            raise TimeoutError("ASR input eligibility elapsed before pipe send")
        connection.send(args)

    async def decode(self, args, deadline):
        try:
            self._require_previous_stop_complete()
            if self.process is None:
                self._start()
            send_deadline = time.monotonic() + max(0.0, deadline - loop_time())
            async with asyncio.timeout_at(deadline):
                await asyncio.to_thread(self._send_before_deadline, self.connection, args, send_deadline)
                # The child now owns its input; neither recv nor stop needs the
                # parent PCM. Engine-owned bytearray aliases are cleared too.
                if isinstance(args[0], bytearray):
                    args[0].clear()
                args = None
                ok, value = await asyncio.to_thread(self.connection.recv)
            if not ok:
                raise RuntimeError(value)
            return value
        except BaseException:
            if args is not None and isinstance(args[0], bytearray):
                args[0].clear()
            args = None
            # stop() defers caller cancellation until confirmed physical exit.
            # Preserve the initiating error (timeout vs explicit cancellation),
            # rather than replacing it with another cancel during cleanup.
            try:
                await self.stop()
            except asyncio.CancelledError:
                pass
            raise


class AsrEngine:
    def __init__(self, config: AsrConfig, *, slots=1, queue_limit=20,
                 decode_timeout_ms=3000, model_sha256=""):
        from .inference_scheduler import WeightedScheduler
        self.config = config
        self.workers = [DecodeWorker() for _ in range(slots)]
        self.scheduler = WeightedScheduler(slots, queue_limit, queue_limit)
        self.decode_timeout_ms = decode_timeout_ms
        self.model_sha256 = model_sha256
        self.model_id = ""
        self.recorder = None
        self._audio_inputs: dict[object, bytearray] = {}
        self.repairs = {}
        self.closing = False

    async def _warm(self, slot):
        await self.workers[slot].decode((bytes(32000), str(self.config.model_dir),
                                        self.config.language, ""), loop_time() + 120)

    async def _ensure_worker(self, slot, deadline):
        # A pending/failed stop cannot be mistaken for an available process.
        # This barrier precedes recorder.started and keeps the original budget.
        stop_task = getattr(self.workers[slot], "_stop_task", None)
        if stop_task is not None:
            try:
                async with asyncio.timeout_at(deadline):
                    await asyncio.shield(stop_task)
            except asyncio.CancelledError:
                if stop_task.cancelled():
                    raise RuntimeError("ASR worker stop was not confirmed") from None
                raise
        if self.workers[slot].process is None and slot not in self.repairs:
            self.repairs[slot] = asyncio.create_task(self._warm(slot))
        repair = self.repairs.get(slot)
        if repair is not None:
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(repair)
            self.repairs.pop(slot, None)

    async def prepare(self):
        folder = self.config.model_dir
        # Match mlx-whisper 0.4.3 load_model exactly: do not pin a different
        # file when both formats are present, or accept its unsupported HF name.
        weights = next((folder / n for n in ('weights.safetensors', 'weights.npz')
                        if (folder / n).is_file()), None)
        if not (folder / 'config.json').is_file() or weights is None:
            raise ValueError('ASR 本地模型不完整；不会自动联网下载')
        def digest():
            h = hashlib.sha256((folder / 'config.json').read_bytes())
            with weights.open('rb') as f:
                for block in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(block)
            return h.hexdigest()
        self.model_id = await asyncio.to_thread(digest)
        if self.model_sha256 and self.model_id != self.model_sha256:
            raise ValueError('ASR 模型摘要与部署清单不符')
        if self.config.prewarm:
            # Synthetic prewarm happens before readiness, outside user budgets.
            await asyncio.gather(*(self._warm(slot) for slot in range(len(self.workers))))

    def release_audio(self, owner, *, expected=None):
        # Synchronous and independent of worker/ledger/database shutdown.
        audio = self._audio_inputs.get(owner)
        if expected is not None and audio is not expected:
            return
        self._audio_inputs.pop(owner, None)
        if audio is not None:
            audio.clear()

    async def decode(self, audio: bytes, prompt: str, *, owner=None,
                     purpose="asr_decode", deadline=None, scope=None, outcome_deadline=None):
        from .inference_context import current_scope, OUTCOME_WRITE_BUDGET_SECONDS
        scope = scope or current_scope.get()
        recorder = self.recorder
        owner = owner or ((scope.user_id, scope.session_id, scope.attempt_no) if scope else ("synthetic", id(audio)))
        deadline = deadline or loop_time() + self.decode_timeout_ms / 1000
        # E gates audio/dispatch/results; B is the original operation budget.
        # Retry adapters can supply B independently of an earlier audio D.
        # The 1s reserve remains inside original B, never subtracted from D.
        outcome_deadline = deadline if outcome_deadline is None else outcome_deadline
        if purpose not in ("asr_decode", "asr_finish") and outcome_deadline != deadline:
            raise ValueError("separate outcome deadline is only valid for ASR")
        if outcome_deadline < deadline:
            raise ValueError("outcome deadline precedes input deadline")
        reserve = OUTCOME_WRITE_BUDGET_SECONDS if purpose in ("asr_decode", "asr_finish") and recorder is not None and scope is not None else 0.0
        execution_deadline = min(deadline, outcome_deadline - reserve)
        if loop_time() >= execution_deadline:
            raise TimeoutError("ASR execution budget exhausted before dispatch")
        outcome = {}
        async def execute(slot):
            nonlocal audio
            if loop_time() >= execution_deadline:
                raise TimeoutError("ASR execution budget exhausted while queued")
            await self._ensure_worker(slot, execution_deadline)
            if loop_time() >= execution_deadline:
                raise TimeoutError("ASR execution budget exhausted preparing worker")
            # Partial factories materialize the newest available window only
            # on dispatch. A queued owner retains no obsolete PCM copy.
            supplied_audio, actual_prompt = audio() if callable(audio) else (audio, prompt)
            actual_audio = bytearray(supplied_audio)
            supplied_audio, audio = b"", b""
            self._audio_inputs[owner] = actual_audio
            call_id, status, error = "", "unknown", "CLIENT_INTERRUPTED"
            started = loop_time()
            actual_deadline = min(execution_deadline, started + self.decode_timeout_ms / 1000) if purpose == "asr_decode" else execution_deadline
            try:
                if actual_deadline <= started:
                    raise TimeoutError("ASR execution budget exhausted materializing input")
                if recorder is not None and scope is not None:
                    async with asyncio.timeout_at(actual_deadline):
                        call_id = await recorder.started(scope, "mlx_whisper", self.model_id, purpose, len(actual_audio) // 2)
                # A started commit is an await boundary, not permission to send
                # audio after D. No next-loop cancellation delivery is assumed.
                if loop_time() >= actual_deadline or self._audio_inputs.get(owner) is not actual_audio:
                    raise TimeoutError("ASR input eligibility elapsed before physical dispatch")
                value = await self.workers[slot].decode((actual_audio, str(self.config.model_dir), self.config.language, actual_prompt), actual_deadline)
                status, error = "succeeded", None
                return value
            except TimeoutError:
                status, error = "failed", "ASR_TIMEOUT"
                raise
            except asyncio.CancelledError:
                status, error = "cancelled", "CANCELLED"
                raise
            except Exception:
                status, error = "failed", "ASR_FAILED"
                raise
            finally:
                self.release_audio(owner, expected=actual_audio)
                actual_audio.clear()
                actual_audio = b""
                if self.workers[slot].process is None and not self.closing and slot not in self.repairs:
                    self.repairs[slot] = asyncio.create_task(self._warm(slot))
                if call_id:
                    outcome.update(call_id=call_id, status=status, error_code=error,
                                   elapsed_ms=int((loop_time() - started) * 1000))
        from .inference_context import persist_outcome
        try:
            result = await self.scheduler.submit(owner, "high" if purpose == "asr_finish" else "low",
                execute, deadline=deadline, replace=purpose == "asr_decode")
        finally:
            audio = b""  # Release supplied finish PCM before any ledger write.
            recorded = await persist_outcome(recorder, outcome, outcome_deadline)
        if not recorded:
            from ..providers.asr.base import ProviderError
            raise ProviderError("ledger_unavailable", "model call outcome could not be persisted")
        return result

    async def cancel(self, owner, *, queued_only=False):
        if not queued_only:
            self.release_audio(owner)
        await self.scheduler.cancel(owner, queued_only=queued_only)

    async def close(self):
        self.closing = True
        await self.scheduler.close()
        for repair in self.repairs.values():
            repair.cancel()
        await asyncio.gather(*self.repairs.values(), return_exceptions=True)
        self.repairs.clear()
        await asyncio.gather(*(worker.stop() for worker in self.workers))


class StreamingText:
    """LocalAgreement from WhisperStreaming; decode scheduling lives in Session.

    Audio positions are absolute samples. Trim only through a confirmed prefix,
    retaining a word of acoustic overlap. finish() does NOT transcribe missing audio.
    """
    def __init__(self):
        self.hypothesis = HypothesisBuffer()
        self.committed = []
        self.revision = 0

    def update(self, result, offset_samples: int):
        self.hypothesis.insert(result['words'], offset_samples / 16000)
        self.committed.extend(self.hypothesis.flush())
        self.revision += 1

    @property
    def stable(self):
        return ''.join(x[2] for x in self.committed).lstrip()

    @property
    def provisional(self):
        return ''.join(x[2] for x in self.hypothesis.complete())

    @property
    def text(self):
        return (self.stable + self.provisional).lstrip()

    @property
    def confirmed_samples(self):
        return round(self.hypothesis.last_commited_time * 16000)

    def prompt(self, offset_samples):
        # Only already-trimmed context belongs in Whisper's initial_prompt. Repeating
        # words still present in the audio window encourages hallucinated repetitions.
        return ''.join(word for _, end, word in self.committed if end <= offset_samples / 16000)[-200:]

    def trim_point(self, result, offset, decoded_samples, trim_seconds):
        if decoded_samples - offset < trim_seconds * 16000 or len(self.committed) < 2:
            return offset
        # Prefer segment boundaries; a word boundary also supports long punctuation-free speech.
        limit = min(self.committed[-2][1], decoded_samples / 16000 - 2)
        candidates = [offset / 16000 + s for s in result['segments'] if offset / 16000 + s <= limit]
        point = max(candidates, default=limit)
        cut = max(offset, round(point * 16000))
        self.hypothesis.pop_commited(cut / 16000)
        return cut
