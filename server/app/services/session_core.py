from __future__ import annotations

import asyncio
import hashlib
import struct
import time
import uuid
from dataclasses import dataclass, field

from .asr import StreamingText


class ProtocolError(ValueError):
    pass


@dataclass
class Session:
    client_request_id: str
    config: object
    engine: object
    refiner: object
    dictionary: object
    epoch: str
    learning_enabled: bool = True
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: str = 'created'
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    audio: bytearray = field(default_factory=bytearray)
    last_seq: int = -1
    total_samples: int = 0
    decoded_samples: int = 0
    offset: int = 0
    ended_at: float | None = None
    cancelled: bool = False
    connected: bool = False
    result: dict | None = None
    error: str | None = None
    insertion: dict | None = None
    task: asyncio.Task | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    events: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(128))
    timeline: list = field(default_factory=list)
    text: StreamingText = field(default_factory=StreamingText)
    corrections: dict = field(default_factory=dict)

    def mark(self, event, **fields):
        self.timeline.append({'event': event, 'at_ms': round((time.monotonic() - self.created_at) * 1000), **fields})

    def emit(self, event, **fields):
        message = {'type': event, 'session_id': self.id, 'service_epoch': self.epoch, **fields}
        if self.events.full():
            # Never drop incoming audio. A client not consuming even status events is failed.
            self.error = '客户端未及时读取状态，请重新连接并重试'
            self.cancelled = True
            return
        self.events.put_nowait(message)

    def public(self):
        return {'session_id': self.id, 'state': self.state, 'service_epoch': self.epoch,
                'last_seq': self.last_seq, 'total_samples': self.total_samples, 'decoded_samples': self.decoded_samples,
                'result': self.result, 'error': self.error, 'timeline': self.timeline}

    def accept(self, frame: bytes):
        if self.ended_at is not None or self.cancelled:
            raise ProtocolError('结束后不能继续提交音频')
        if not 8 < len(frame) <= 8 + self.config.audio.max_chunk_samples * 2:
            raise ProtocolError('音频块大小无效')
        seq, count = struct.unpack_from('<II', frame)
        if count * 2 != len(frame) - 8 or count == 0 or seq != self.last_seq + 1:
            raise ProtocolError('音频序号或样本长度不连续，拒绝生成缺失内容的结果')
        if self.total_samples + count > self.config.audio.max_duration_seconds * 16000:
            raise ProtocolError('超过本轮录音时长上限')
        self.audio.extend(frame[8:])
        self.total_samples += count
        self.last_seq = seq
        self.state = 'streaming'
        self.updated_at = time.monotonic()
        if seq == 0:
            self.mark('first_audio')
        self.emit('audio.ack', seq=seq, total_samples=self.total_samples)
        self.wake.set()

    def end(self, last_seq: int, total_samples: int):
        if last_seq != self.last_seq or total_samples != self.total_samples:
            raise ProtocolError('结束标记与已接收音频不一致')
        if self.ended_at is not None:
            return
        self.ended_at = time.monotonic()
        self.updated_at = self.ended_at
        self.state = 'finalizing'
        self.mark('audio.end', total_samples=total_samples)
        self.wake.set()

    def cancel(self):
        if self.cancelled:
            return
        self.cancelled = True
        self.state = 'cancelled'
        self.result = None
        self.insertion = None
        self.audio.clear()
        self.emit('cancelled')
        self.wake.set()

    async def run(self):
        try:
            while not self.cancelled:
                await self.wake.wait()
                self.wake.clear()
                ended = self.ended_at is not None
                if not ended and self.total_samples - self.decoded_samples < self.config.asr.step_seconds * 16000:
                    continue
                if self.total_samples > self.decoded_samples:
                    snapshot_end = self.total_samples
                    if (snapshot_end - self.offset) > 60 * 16000:
                        raise ProtocolError('识别积压超过 60 秒，请缩短口述后重试')
                    self.mark('asr.start', from_sample=self.offset, to_sample=snapshot_end, after_end=ended)
                    # A copy freezes the window while the WebSocket continues receiving chunks.
                    result = await self.engine.decode(bytes(self.audio[self.offset * 2:snapshot_end * 2]), self.text.prompt(self.offset))
                    if self.cancelled:
                        return
                    self.text.update(result, self.offset)
                    self.decoded_samples = snapshot_end
                    self.mark('asr.end', to_sample=snapshot_end, confirmed_samples=self.text.confirmed_samples)
                    self.offset = self.text.trim_point(result, self.offset, snapshot_end, self.config.asr.trim_seconds)
                    self.emit('asr.partial', revision=self.text.revision, stable_text=self.text.stable,
                              provisional_text=self.text.provisional, decoded_samples=snapshot_end,
                              received_samples=self.total_samples, confirmed_samples=self.text.confirmed_samples)
                if self.ended_at is not None and self.decoded_samples == self.total_samples:
                    break
                if self.total_samples - self.decoded_samples >= self.config.asr.step_seconds * 16000 or self.ended_at is not None:
                    self.wake.set()
            if self.cancelled:
                return
            raw = self.text.text
            if not raw:
                raise ProtocolError('没有识别到语音，请检查麦克风后重试')
            self.emit('asr.final', text=raw)
            self.state = 'refining'
            self.mark('refine.start')
            terms = self.dictionary.relevant(raw, self.config.dictionary.max_prompt_terms)
            try:
                revised, review = await self.refiner.refine(raw, terms)
            except Exception:
                revised, review = raw, '整理服务未返回有效结果；已保留原始转写，请确认后使用'
            if self.cancelled:
                return
            self.mark('refine.end')
            self.result = {'result_id': str(uuid.uuid4()), 'text': revised, 'raw_text': raw,
                'review_required': bool(review), 'review_reason': review,
                'text_hash': hashlib.sha256(revised.encode()).hexdigest(),
                'dictionary_version': self.dictionary.version, 'used_term_ids': [t['id'] for t in terms],
                'config_hash': self.config.digest, 'asr_model': self.engine.model_id,
                'refiner_model': self.refiner.digest,
                'stop_to_result_ms': round((time.monotonic() - self.ended_at) * 1000)}
            self.state = 'ready'
            self.updated_at = time.monotonic()
            self.audio.clear()
            self.emit('result.ready', result=self.result)
        except asyncio.CancelledError:
            self.cancel()
            raise
        except Exception as e:
            if not self.cancelled:
                self.state = 'failed'
                self.error = str(e) if isinstance(e, (ProtocolError, ValueError)) else '本地识别失败，请检查模型与运行环境'
                self.updated_at = time.monotonic()
                self.emit('error', message=self.error)

    async def watch_timeout(self):
        # Timeout starts at audio.end, independent of the maximum recording duration.
        while self.task and not self.task.done():
            await asyncio.sleep(0.25)
            if self.ended_at is not None and time.monotonic() - self.ended_at > self.config.runtime.pipeline_timeout_seconds:
                self.error = '停止后处理超时，原始结果未自动插入'
                self.cancel()
                self.state = 'failed'
                self.emit('error', message=self.error)
                return
            if self.ended_at is None and time.monotonic() - self.updated_at > 15:
                self.error = '音频连接中断或未开始，录音已取消'
                self.cancel()
                self.emit('error', message=self.error)
                return
