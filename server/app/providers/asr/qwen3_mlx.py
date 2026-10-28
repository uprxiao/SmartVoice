"""Production native Qwen streaming over the owned, killable ASR worker."""
from __future__ import annotations

import asyncio
from pathlib import Path

from ...core.clock import loop_time
from ...services.asr_core import DecodeWorker
from ...services.inference_context import CallScope, OUTCOME_WRITE_BUDGET_SECONDS, persist_outcome
from ...services.inference_scheduler import WeightedScheduler
from .base import AsrCapabilities, AsrFinal, AsrOpenOptions, AsrPartial, AsrSegment, ProviderError
from .qwen_manifest import validate_model_manifest
from .qwen_checkpoint import clear as clear_checkpoint, validate as validate_checkpoint
from .qwen_worker import qwen_worker_main

CHUNK_SAMPLES = 32000


class NativeGenerationLost(RuntimeError):
    """A healthy owner was interrupted by another owner's process cleanup."""


class QwenEngine:
    def __init__(self, config, *, queue_limit=20, worker=None):
        if config.execution_slots != 1:
            raise ValueError('qwen3_mlx currently requires execution_slots=1')
        if not config.model_manifest_path:
            raise ValueError('qwen3_mlx requires model_manifest_path')
        self.config = config
        self.worker = worker or DecodeWorker(target=qwen_worker_main)
        self.workers = [self.worker]
        self.scheduler = WeightedScheduler(1, queue_limit, queue_limit)
        self.recorder = None
        self.sessions = {}
        self._audio_inputs = {}
        self.model_id = ''
        self.runtime_identity = {}
        self.ready = self.closing = False
        self.generation = 0
        self.repair = self._poison_task = None

    async def prepare(self):
        if not self.model_id:
            digest = await asyncio.to_thread(validate_model_manifest, self.config.model_path,
                self.config.model_manifest_path, self.config.model_sha256)
            self.model_id = (self.config.model_id or 'qwen3-asr-1.7b') + '@' + digest
        await self._warm()

    async def _warm(self):
        self.ready = False
        result = await self.worker.decode((b'', {'action': 'prepare',
            'model_path': str(Path(self.config.model_path).resolve()), 'runtime_path': self.config.runtime_path}),
            loop_time()+120)
        if result.get('ready') is not True:
            raise ProviderError('unavailable', 'Qwen synthetic prewarm failed')
        self.runtime_identity = result
        if self.generation == 0:
            self.generation = 1
        self.ready = not self.closing

    def release_audio(self, owner):
        value = self._audio_inputs.pop(owner, None)
        if isinstance(value,bytearray):
            value.clear()

    async def poison(self, failed=None, *, preserve_live_timeout=False):
        resumable = (preserve_live_timeout and failed is not None
            and failed.options.outcome_deadline is None and not failed.cancelled
            and not failed.error and not self.closing)
        if failed is not None and not resumable:
            failed.release_checkpoint()
        # A physical stop discards native caches, not the identity/audio of
        # unrelated owners. Their last acknowledged bounded states can recover.
        if self._poison_task is None or (self._poison_task.done() and self.ready):
            self.ready = False
            self.generation += 1
            for stream in list(self.sessions.values()):
                if ((stream is failed and not resumable) or stream.cancelled or stream.done or stream.error):
                    continue
                if failed is None or (stream.decoded_samples and stream.checkpoint is None):
                    stream.invalidate(ProviderError('unavailable', 'Qwen native checkpoint unavailable', retryable=True))
                else:
                    stream.generation = self.generation
            for owner in list(self._audio_inputs):
                self.release_audio(owner)
            self._poison_task = asyncio.create_task(self.worker.stop())
        interrupted = None
        while not self._poison_task.done():
            try:
                await asyncio.shield(self._poison_task)
            except asyncio.CancelledError as exc:
                interrupted = exc
        self._poison_task.result()  # Failed stop remains quarantined.
        if not self.closing and (self.repair is None or self.repair.done()):
            self.repair = asyncio.create_task(self._warm())
            # Retrieve failure without losing health's access to the failed task.
            self.repair.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        if interrupted is not None:
            raise interrupted

    async def ensure_ready(self, stream, deadline):
        stream._check()
        if not self.ready:
            # Shared preparation remains alive if this individual waiter times
            # out or is cancelled. Its wait consumes the original call budget.
            async with asyncio.timeout_at(deadline):
                if self._poison_task is not None:
                    await asyncio.shield(self._poison_task)
                if self.repair is not None:
                    await asyncio.shield(self.repair)
        stream._check()
        if not self.ready or self.closing:
            raise ProviderError('unavailable', 'Qwen worker recovery is not ready', retryable=True)

    async def run(self, stream, action, take, deadline, *, outcome_deadline=None):
        original = outcome_deadline if outcome_deadline is not None else deadline
        if original < deadline:
            raise ValueError('outcome deadline precedes eligibility deadline')
        scope, recorder = stream.scope, self.recorder
        reserve = OUTCOME_WRITE_BUDGET_SECONDS if recorder is not None and scope is not None else 0
        execution_deadline = min(deadline, original-reserve)
        outcome = {}

        async def execute(slot):
            await self.ensure_ready(stream, execution_deadline)
            if not self.ready or stream.generation != self.generation or stream.error or stream.cancelled:
                raise ProviderError('unavailable', 'Qwen session generation is unavailable')
            if loop_time() >= execution_deadline:
                raise TimeoutError('ASR input eligibility elapsed')
            supplied = take()
            audio = bytearray(supplied) if supplied else b''
            supplied = None
            self._audio_inputs[stream.owner] = audio
            started = loop_time()
            dispatch_generation = self.generation
            physical_deadline = min(execution_deadline, started+self.config.decode_timeout_ms/1000) if action == 'feed' else execution_deadline
            call_id, status, error = '', 'unknown', 'CLIENT_INTERRUPTED'
            result = None
            try:
                # An exact-chunk finish performs no further model decode. The
                # control RPC closes its state without a fictitious model call.
                physical = bool(audio)
                if physical and recorder is not None and scope is not None:
                    async with asyncio.timeout_at(physical_deadline):
                        call_id = await recorder.started(scope, 'qwen3_mlx', self.model_id,
                            'asr_finish' if action == 'finish' else 'asr_decode', len(audio)//2)
                if (loop_time() >= physical_deadline or self._audio_inputs.get(stream.owner) is not audio
                        or stream.cancelled or stream.error):
                    raise TimeoutError('ASR input eligibility elapsed before dispatch')
                command = {'action': action, 'owner': stream.owner,
                    'start_sample': stream.decoded_samples, 'options': stream.native_options}
                if stream.has_worker_state and stream.native_generation != dispatch_generation:
                    command['checkpoint'] = validate_checkpoint(stream.checkpoint, stream.owner, stream.decoded_samples)
                result = await self.worker.decode((audio, command), physical_deadline)
                command = None
                if dispatch_generation != self.generation and not stream.cancelled and not stream.error:
                    raise NativeGenerationLost('healthy Qwen native state must be restored')
                if stream.generation != self.generation or stream.cancelled or stream.error:
                    raise ProviderError('cancelled', 'stale Qwen output')
                status, error = 'succeeded', None
                return result, dispatch_generation
            except BaseException as exc:
                if isinstance(result, dict):
                    clear_checkpoint(result.get('checkpoint'))
                result = command = None
                expired = (isinstance(exc, TimeoutError) or (isinstance(exc, asyncio.CancelledError)
                    and loop_time() >= physical_deadline and not stream.cancelled and not self.closing))
                status = 'cancelled' if isinstance(exc, asyncio.CancelledError) and not expired else 'failed'
                error = 'ASR_TIMEOUT' if expired else 'CANCELLED' if status == 'cancelled' else 'ASR_FAILED'
                if (not isinstance(exc, asyncio.CancelledError) and not stream.cancelled and not stream.error
                        and not self.closing and dispatch_generation != self.generation
                        and stream.generation == self.generation):
                    raise NativeGenerationLost('healthy Qwen request interrupted by another owner') from exc
                # DecodeWorker owns physical stop; poison also handles fake
                # workers, pre-dispatch faults and sibling state invalidation.
                await self.poison(stream, preserve_live_timeout=(action == 'feed' and expired))
                raise
            finally:
                self.release_audio(stream.owner)
                if isinstance(audio, bytearray):
                    audio.clear()
                if call_id:
                    outcome.update(call_id=call_id, status=status, error_code=error,
                        elapsed_ms=int((loop_time()-started)*1000))

        try:
            answer, dispatch_generation = await self.scheduler.submit(stream.owner, 'high' if action == 'finish' else 'low',
                execute, deadline=execution_deadline)
        finally:
            recorded = await persist_outcome(recorder, outcome, original)
        if not recorded:
            if isinstance(answer, dict):
                clear_checkpoint(answer.get('checkpoint'))
            await self.poison(stream)
            raise ProviderError('ledger_unavailable', 'Qwen model outcome was not persisted')
        if dispatch_generation != self.generation:
            if isinstance(answer, dict):
                clear_checkpoint(answer.get('checkpoint'))
            raise NativeGenerationLost('Qwen generation changed before acknowledgement')
        return answer

    async def cancel(self, stream):
        stream.release_checkpoint()
        self.release_audio(stream.owner)
        await self.scheduler.cancel(stream.owner)
        if stream.has_worker_state and stream.native_generation == self.generation and self.ready:
            deadline = loop_time()+.5
            async def drop(slot):
                answer = await self.worker.decode((b'', {'action': 'drop', 'owner': stream.owner}), deadline)
                if answer.get('dropped') is not True:
                    raise ProviderError('invalid_output', 'Qwen state drop was not confirmed')
                return answer
            try:
                await self.scheduler.submit((*stream.owner, 'release'), 'high', drop, deadline=deadline)
            except BaseException:
                # Waiting for another user's decode cannot postpone the audio
                # cleanup deadline. Fall back to confirmed generation exit.
                await self.poison(stream)
        self.sessions.pop(stream.owner, None)

    async def close(self):
        self.closing = True
        self.ready = False
        for stream in list(self.sessions.values()):
            stream.invalidate(ProviderError('cancelled', 'provider closing'))
        for owner in list(self._audio_inputs):
            self.release_audio(owner)
        await self.scheduler.close()
        if self.repair is not None:
            self.repair.cancel()
            await asyncio.gather(self.repair, return_exceptions=True)
        await self.worker.stop()
        self.sessions.clear()


class QwenAsrSession:
    def __init__(self, engine, options: AsrOpenOptions):
        self.engine, self.options = engine, options
        self.owner = (options.user_id, options.session_id, options.attempt_no)
        self.scope = CallScope(*self.owner) if options.user_id else None
        self.generation = engine.generation
        self.native_generation = engine.generation
        self.checkpoint = None
        self.pending = bytearray()
        self.received_samples = self.decoded_samples = self.revision = 0
        self.stable = self.text = ''
        self.error = None
        self.cancelled = self.finishing = self.done = self.has_worker_state = False
        self.queue = asyncio.Queue(maxsize=8)
        self.task = None
        self.wake = asyncio.Event()
        self.progress = asyncio.Event()
        # Bound parent backlog independently of the full retry cache. Normal
        # live ACK never waits for inference; overload fails explicitly. An
        # internal cached-audio replay may apply backpressure in its own budget.
        self.pending_limit_samples = CHUNK_SAMPLES + (engine.config.decode_timeout_ms*16000)//1000
        terms, used = [], 0
        for term in options.hint_terms:
            cost = len(term) + (2 if terms else 0)
            if len(terms) < 100 and used+cost <= 2000:
                terms.append(term); used += cost
        language = {'auto': None, 'zh': 'Chinese', 'en': 'English'}.get(options.language, options.language)
        self.native_options = {'context': ', '.join(terms), 'language': language}
        engine.sessions[self.owner] = self

    def _put(self, item):
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(item)

    def invalidate(self, error):
        if self.done or self.cancelled:
            return
        if self.error is None:
            self.error = error
        self.pending.clear()
        self.release_checkpoint()
        self.native_options = {'context': '', 'language': None}
        self._put(self.error)
        self.wake.set()
        self.progress.set()

    def release_checkpoint(self):
        clear_checkpoint(self.checkpoint)
        self.checkpoint = None

    def _check(self):
        if self.cancelled:
            raise ProviderError('cancelled', 'Qwen session cancelled')
        if self.error:
            raise self.error
        if self.generation != self.engine.generation:
            raise ProviderError('unavailable', 'Qwen session generation changed')

    async def feed(self, pcm_s16le, start_sample):
        self._check()
        if self.finishing or self.done or len(pcm_s16le)%2 or start_sample != self.received_samples:
            raise ProviderError('invalid_input', 'Qwen samples are not contiguous PCM')
        samples = len(pcm_s16le)//2
        if self.received_samples+samples > self.options.max_samples:
            raise ProviderError('invalid_input', 'Qwen audio exceeds session limit')
        while len(self.pending)//2+samples > self.pending_limit_samples:
            if self.options.outcome_deadline is None:
                error = ProviderError('resource_exhausted','Qwen live decode backlog exceeded',retryable=True)
                self.invalidate(error)
                raise error
            self.progress.clear()
            async with asyncio.timeout_at(min(self.options.outcome_deadline,self.options.eligibility_deadline or float('inf'))):
                await self.progress.wait()
            self._check()
            if self.finishing:
                raise ProviderError('invalid_input','Qwen feed arrived after finish')
        self.pending.extend(pcm_s16le)
        self.received_samples += samples
        self.wake.set()
        if self.task is None:
            self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        try:
            while not self.finishing and not self.cancelled and not self.error:
                if len(self.pending)//2 < CHUNK_SAMPLES:
                    self.wake.clear()
                    await self.wake.wait()
                    continue
                deadline = loop_time()+self.engine.config.decode_timeout_ms/1000
                if self.options.outcome_deadline is not None:
                    deadline = min(deadline,self.options.outcome_deadline)
                eligibility = min(deadline,self.options.eligibility_deadline or float('inf'))
                received_at_dispatch = self.received_samples
                try:
                    await self._decode('feed', CHUNK_SAMPLES, eligibility, outcome_deadline=deadline)
                except TimeoutError:
                    if self.options.outcome_deadline is not None:
                        raise  # A cached replay never gains another operation budget.
                    self._check()
                    # A failed provisional decode is not a failed dictation.
                    # Keep its unacknowledged PCM. New incoming audio may start
                    # the next live operation; with no new data wait for it or
                    # authoritative finish instead of busy-retrying this call.
                    while (not self.finishing and not self.cancelled and not self.error
                           and self.received_samples == received_at_dispatch):
                        self.wake.clear()
                        await self.wake.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.error:
                self.invalidate(exc if isinstance(exc, ProviderError) else ProviderError('unavailable','Qwen decode failed',retryable=True))

    async def _decode(self, action, count, deadline, *, outcome_deadline=None):
        self._check()
        def pcm():
            self._check()
            return bytes(self.pending[:count*2])
        while True:
            try:
                result = await self.engine.run(self,action,pcm,deadline,outcome_deadline=outcome_deadline)
                break
            except NativeGenerationLost:
                self._check()
                if loop_time() >= deadline:
                    raise TimeoutError('Qwen recovery exhausted the original ASR deadline')
        self._check()
        text, stable, samples = result.get('text'),result.get('stable'),result.get('samples')
        if (not isinstance(text,str) or not isinstance(stable,str) or type(samples) is not int
                or samples != self.decoded_samples+count or samples > self.received_samples
                or not text.startswith(stable) or not stable.startswith(self.stable)
                or (action=='finish' and not text.startswith(self.stable))):
            error = ProviderError('invalid_output','Qwen partial/stable/sample contract violated')
            self.invalidate(error)
            clear_checkpoint(result.get('checkpoint'))
            await self.engine.poison(self)
            raise error
        checkpoint = result.get('checkpoint')
        try:
            if action == 'feed':
                validate_checkpoint(checkpoint, self.owner, samples)
                fields = checkpoint['fields']
                if (fields['context'] != self.native_options['context']
                        or (fields['text'] if checkpoint['nonzero'] else '') != text
                        or (fields['committed_text'] if checkpoint['nonzero'] else '') != stable):
                    raise ValueError('Qwen checkpoint does not match its response or options')
            elif checkpoint is not None:
                raise ValueError('Qwen finish retained a native checkpoint')
        except ValueError as exc:
            clear_checkpoint(checkpoint)
            error = ProviderError('invalid_output', 'Qwen checkpoint contract violated')
            self.invalidate(error)
            await self.engine.poison(self)
            raise error from exc
        self.release_checkpoint()
        self.checkpoint = checkpoint
        del self.pending[:count*2]
        self.progress.set()
        self.decoded_samples = samples
        self.text, self.stable = text, stable
        self.has_worker_state = action != 'finish'
        self.native_generation = self.engine.generation
        self.revision += 1
        if action != 'finish':
            self._put(AsrPartial(self.revision,stable,text[len(stable):],samples,self.received_samples))

    async def partials(self):
        while not self.cancelled:
            item = await self.queue.get()
            if isinstance(item,BaseException):
                raise item
            yield item

    async def finish(self,total_samples,deadline):
        self._check()
        if self.done or total_samples != self.received_samples:
            raise ProviderError('invalid_input','Qwen finish sample count mismatch')
        self.finishing = True
        self.wake.set()
        self.progress.set()
        original = self.options.outcome_deadline or deadline
        try:
            await self.engine.scheduler.cancel(self.owner,queued_only=True)
            if self.task:
                async with asyncio.timeout_at(deadline):
                    await asyncio.shield(asyncio.gather(self.task,return_exceptions=True))
            self._check()
            # Promote all undelivered PCM with the stop request. The native
            # worker retains its own 2-second chunking before finishing; do not
            # put stopped whole chunks back into the live/low-priority queue.
            await self._decode('finish',len(self.pending)//2,deadline,outcome_deadline=original)
            self.done = True
            self.pending.clear()
            self.engine.sessions.pop(self.owner,None)
            self.native_options = {'context':'','language':None}
            return AsrFinal(self.text,[AsrSegment(0,total_samples,self.text)] if self.text else [],
                not self.text,self.engine.model_id,total_samples)
        except TimeoutError as exc:
            await self.cancel()
            raise ProviderError('timeout','Qwen finish deadline elapsed',retryable=True) from exc

    async def cancel(self):
        self.cancelled = True
        self.pending.clear()  # Synchronous before any queue/worker/ledger await.
        self.release_checkpoint()
        self.native_options = {'context':'','language':None}
        self.engine.release_audio(self.owner)
        self.wake.set()
        self.progress.set()
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
        await self.engine.cancel(self)
        self.text = self.stable = ''
        while not self.queue.empty():
            self.queue.get_nowait()


class Qwen3MlxProvider:
    capabilities = AsrCapabilities(backend_version='mlx-qwen3-asr/0.4.4')

    def __init__(self,config,*,queue_limit=20,worker=None):
        self.model_id = config.model_id
        self.engine = QwenEngine(config,queue_limit=queue_limit,worker=worker)

    @property
    def scheduler(self):return self.engine.scheduler

    @property
    def recorder(self):return self.engine.recorder

    @recorder.setter
    def recorder(self,value):self.engine.recorder=value

    async def prepare(self):
        try:await self.engine.prepare()
        except Exception as exc:raise ProviderError('unavailable','Qwen prepare failed') from exc

    async def open(self,options):
        if not self.engine.ready or self.engine.closing:
            raise ProviderError('unavailable','Qwen is not prepared')
        owner=(options.user_id,options.session_id,options.attempt_no)
        if owner in self.engine.sessions:
            raise ProviderError('invalid_input','Qwen session already open')
        return QwenAsrSession(self.engine,options)

    async def health(self):
        return (self.engine.ready and not self.engine.worker.stop_incomplete
            and self.engine.worker.process is not None and self.engine.worker.process.is_alive())

    async def close(self):await self.engine.close()
