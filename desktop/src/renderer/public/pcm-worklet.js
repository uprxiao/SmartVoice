// 16 kHz PCM with one end-to-end server-ACK credit window.
// Windowed-sinc/Hann kernel, radius=24 and cutoff=.45/ratio adapted from our
// earlier SmartDD prototype, src/renderer/core/voice/pcm.ts
// SHA256 ff6a4ed480bf84ab4480c2fd60c66fbe3b0d67bc96aa21b3e1b660aab257b952.
// Mono averaging follows that project's audio-worklet.ts, SHA256
// f744415713050c7f34852e2dbb8f3336e6758aaaf4a2a51ad0546d66dbdd1496.
// Adaptations: fixed ring/phase table, integer phase, shared ACK credit and
// ceil(N*16000/sourceRate) output count. Endpoint extension repeats the first/
// last input value; no filter tail is emitted beyond the captured duration.
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.inputRate = sampleRate;
    this.supportedRate = [16000, 44100, 48000].includes(this.inputRate);
    this.chunkSamples = 4000;
    this.maxSamples = 2880000;
    this.maxInputSamples = this.maxSamples * this.inputRate / this.targetRate;
    this.maxUnacknowledged = 32000;
    this.maxUnacknowledgedSeconds = 2;
    this.radius = 24;
    this.ring = new Float32Array(64);
    this.inputTimes = new Float64Array(64);
    this.inputSamples = 0;
    this.firstValue = this.lastValue = 0;
    this.lastInputTime = null;
    this.kernels = [];
    this.phaseUnit = 16000;
    if (this.supportedRate && this.inputRate !== 16000) this.prepareKernels();
    this.pending = new Float32Array(this.chunkSamples);
    this.filled = 0;
    this.pendingSince = null;
    this.totalSamples = 0;
    this.sentSamples = 0;
    this.acknowledgedSamples = 0;
    this.frames = [];
    this.stopped = false;
    this.failed = false;
    this.levelAccumulator = 0;
    this.levelCount = 0;
    this.lastLevelAt = currentTime;
    this.lastQuantumAt = null;
    // Graph construction/resume may span render quanta. No audio belongs to
    // this capture until an explicit begin is confirmed by process().
    this.captureState = 'unprepared';
    this.preparationQuanta = 0;
    this.preparationLastInputTime = null;
    this.preparationLastQuantumAt = null;
    this.port.onmessage = event => {
      if (this.stopped) return;
      const data = event.data;
      if (data?.type === 'discard') { this.stopped = true; this.clear(); }
      else if (data?.type === 'prepare' && this.captureState === 'unprepared') {
        this.captureState = 'preparing';
      } else if (data?.type === 'begin' && this.captureState === 'ready') {
        // V2: per-session limit (whole seconds, ≤360 s). Absent = V1 180 s.
        if (data.max_samples !== undefined) {
          const max = data.max_samples;
          if (!Number.isSafeInteger(max) || max < 16000 || max > 5760000 || max % 16000 !== 0) {
            this.fail('AUDIO_PROTOCOL_ERROR', '录音上限无效，已停止本轮录音'); return;
          }
          this.maxSamples = max;
          this.maxInputSamples = max * this.inputRate / this.targetRate;
        }
        this.captureState = 'begin_pending';
      } else if (data?.type === 'ack' && this.captureState === 'recording') this.acknowledge(data.total_samples);
      else if (data?.type === 'stop') {
        if (this.captureState !== 'recording') { this.stopped = true; this.clear(); }
        else if (this.checkDeadline(currentTime)) this.complete(false);
      } else this.fail('AUDIO_PROTOCOL_ERROR', '音频确认消息无效，已停止本轮录音');
    };
  }

  prepareKernels() {
    let a = this.inputRate, b = this.targetRate;
    while (b !== 0) { const remainder = a % b; a = b; b = remainder; }
    this.phaseUnit = a;
    const cutoff = 0.45 * this.targetRate / this.inputRate;
    // 44.1 kHz needs 160 phases; 48 kHz needs one. No trigonometry, growing
    // buffers, buffer slicing or phase truncation occurs in process().
    for (let phase = 0; phase < this.targetRate / this.phaseUnit; phase++) {
      const fraction = phase * this.phaseUnit / this.targetRate;
      const weights = new Float64Array(this.radius * 2);
      let sum = 0;
      for (let tap = 0; tap < weights.length; tap++) {
        const distance = tap + 1 - this.radius - fraction;
        const sinc = distance === 0 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * distance) / (Math.PI * distance);
        const weight = sinc * (0.5 + 0.5 * Math.cos(Math.PI * distance / this.radius));
        weights[tap] = weight; sum += weight;
      }
      for (let tap = 0; tap < weights.length; tap++) weights[tap] /= sum;
      this.kernels.push(weights);
    }
  }

  capturedOutputSamples(inputCount = this.inputSamples) {
    // All operands are safe integers for the supported rates and the 360 s
    // V2 ceiling (5,760,000 × 48,000 ≈ 2.8e11 < 2^53).
    return Math.floor((inputCount * this.targetRate + this.inputRate - 1) / this.inputRate);
  }

  oldestUnacknowledgedAt() {
    if (this.frames.length > 0) return this.frames[0].contextTime;
    if (this.pendingSince !== null) return this.pendingSince;
    // Filter lookahead is also captured audio. It consumes both sample credit
    // and age budget even before enough future input exists to emit one sample.
    if (this.capturedOutputSamples() > this.totalSamples) {
      const numerator = this.totalSamples * this.inputRate;
      const index = Math.floor(numerator / this.targetRate), remainder = numerator % this.targetRate;
      return this.inputTimes[index % this.inputTimes.length] + remainder / this.targetRate / this.inputRate;
    }
    return null;
  }

  checkDeadline(now) {
    const oldest = this.oldestUnacknowledgedAt();
    if (!Number.isFinite(now) || now < 0) {
      this.fail('AUDIO_PROTOCOL_ERROR', '音频采样时钟无效，已停止本轮录音'); return false;
    }
    if (oldest !== null && now - oldest >= this.maxUnacknowledgedSeconds) {
      this.fail('AUDIO_BACKPRESSURE', '音频确认等待超过 2 秒，已停止本轮录音'); return false;
    }
    return true;
  }

  acknowledge(total) {
    if (!this.checkDeadline(currentTime)) return;
    if (!Number.isSafeInteger(total) || total < this.acknowledgedSamples || total > this.sentSamples
      || (total !== this.acknowledgedSamples && !this.frames.some(frame => frame.total === total))) {
      this.fail('AUDIO_PROTOCOL_ERROR', '音频累计确认不匹配，已停止本轮录音'); return;
    }
    this.acknowledgedSamples = total;
    this.frames = this.frames.filter(frame => frame.total > total);
  }

  prepareCapture(channels, quantumAt) {
    if (!Number.isFinite(quantumAt) || quantumAt < 0 || (this.preparationLastQuantumAt !== null
      && quantumAt < this.preparationLastQuantumAt)) {
      this.fail('AUDIO_PROTOCOL_ERROR', '音频采样格式或时钟无效，已停止本轮录音'); return false;
    }
    this.preparationLastQuantumAt = quantumAt;
    if (this.captureState === 'unprepared') return true;
    if (channels && channels.length > 0 && (channels.length > 2 || !channels[0]
      || (channels.length === 2 && channels[1].length !== channels[0].length))) {
      this.fail('AUDIO_PROTOCOL_ERROR', '音频声道格式无效，已停止本轮录音'); return false;
    }
    if (!channels || channels.length === 0 || channels[0].length === 0) {
      if (this.captureState === 'preparing') {
        this.preparationQuanta = 0; this.preparationLastInputTime = null;
      }
      return true;
    }
    if (channels.length > 2 || !channels[0]
      || (channels.length === 2 && channels[1].length !== channels[0].length)) {
      this.fail('AUDIO_PROTOCOL_ERROR', '音频声道格式无效，已停止本轮录音'); return false;
    }
    // Eligibility is finite, nonempty input, including digital silence.
    for (const channel of channels) {
      for (let i = 0; i < channel.length; i++) {
        if (!Number.isFinite(channel[i])) {
          this.fail('AUDIO_PROTOCOL_ERROR', '音频样本无效，已停止本轮录音'); return false;
        }
      }
    }
    const continuous = this.preparationLastInputTime === null ||
      Math.abs(quantumAt - this.preparationLastInputTime - 1 / this.inputRate) <= 1 / this.inputRate;
    if (!continuous && this.captureState !== 'preparing') {
      // Once ready was announced, the begin boundary may not hide a gap.
      this.fail('AUDIO_INTERRUPTED', '音频采样不连续，已停止本轮录音'); return false;
    }
    if (this.captureState === 'begin_pending') {
      // The original capture counters/ring/credit have never consumed input.
      // Keep the preceding real boundary for the original continuity guard.
      this.lastInputTime = this.preparationLastInputTime;
      this.captureState = 'recording';
      this.lastLevelAt = quantumAt;
      this.port.postMessage({ type: 'begun', context_time: quantumAt });
      return true;
    }
    this.preparationQuanta = continuous ? this.preparationQuanta + 1 : 1;
    this.preparationLastInputTime = quantumAt + (channels[0].length - 1) / this.inputRate;
    if (this.captureState === 'preparing' && this.preparationQuanta >= 2) {
      this.captureState = 'ready';
      this.port.postMessage({ type: 'ready' });
    }
    return true;
  }

  process(inputs) {
    if (this.stopped) return false;
    const quantumAt = currentTime;
    if (!this.supportedRate || sampleRate !== this.inputRate
      || (this.lastQuantumAt !== null && quantumAt < this.lastQuantumAt)) {
      this.fail('AUDIO_PROTOCOL_ERROR', '音频采样格式或时钟无效，已停止本轮录音'); return false;
    }
    const channels = inputs[0];
    if (this.captureState !== 'recording') {
      if (!this.prepareCapture(channels, quantumAt)) return false;
      if (this.captureState !== 'recording') return true;
    }
    if (!this.checkDeadline(quantumAt)) return false;
    this.lastQuantumAt = quantumAt;
    if (!channels || channels.length === 0) return true;
    if (channels.length > 2 || !channels[0] || (channels.length === 2 && channels[1].length !== channels[0].length)) {
      this.fail('AUDIO_PROTOCOL_ERROR', '音频声道格式无效，已停止本轮录音'); return false;
    }
    if (this.lastInputTime !== null && channels[0].length > 0
      && Math.abs(quantumAt - this.lastInputTime - 1 / this.inputRate) > 1 / this.inputRate) {
      this.fail('AUDIO_INTERRUPTED', '音频采样不连续，已停止本轮录音'); return false;
    }
    for (let i = 0; i < channels[0].length; i++) {
      const sampleAt = quantumAt + i / this.inputRate;
      if (!this.checkDeadline(sampleAt)) return false;
      if (this.capturedOutputSamples(this.inputSamples + 1) - this.acknowledgedSamples > this.maxUnacknowledged) {
        this.fail('AUDIO_BACKPRESSURE', '未确认音频达到 32000 样本，已停止本轮录音'); return false;
      }
      let value = channels[0][i];
      if (channels.length === 2) value = (value + channels[1][i]) / 2;
      if (!Number.isFinite(value)) {
        this.fail('AUDIO_PROTOCOL_ERROR', '音频样本无效，已停止本轮录音'); return false;
      }
      if (this.inputSamples === 0) this.firstValue = value;
      this.lastValue = value; this.lastInputTime = sampleAt;
      const slot = this.inputSamples % this.ring.length;
      this.ring[slot] = value; this.inputTimes[slot] = sampleAt; this.inputSamples++;
      if (!this.drain(false)) return false;
      if (this.inputSamples === this.maxInputSamples) {
        this.complete(true); return false;
      }
    }
    if (quantumAt - this.lastLevelAt >= 0.05) {
      const rms = Math.sqrt(this.levelAccumulator / Math.max(1, this.levelCount));
      this.port.postMessage({ type: 'level', level: Math.min(1, rms * 8) });
      this.levelAccumulator = 0; this.levelCount = 0; this.lastLevelAt = quantumAt;
    }
    return true;
  }

  drain(final) {
    const limit = Math.min(this.maxSamples, this.capturedOutputSamples());
    while (this.totalSamples < limit) {
      const numerator = this.totalSamples * this.inputRate;
      const index = Math.floor(numerator / this.targetRate), remainder = numerator % this.targetRate;
      if (!final && this.inputRate !== this.targetRate && index + this.radius >= this.inputSamples) break;
      const sampleAt = this.inputTimes[index % this.inputTimes.length]
        + remainder / this.targetRate / this.inputRate;
      let value = 0;
      if (this.inputRate === this.targetRate) value = this.ring[index % this.ring.length];
      else {
        const kernel = this.kernels[remainder / this.phaseUnit];
        for (let tap = 0; tap < kernel.length; tap++) {
          const sourceIndex = index + tap + 1 - this.radius;
          const input = sourceIndex < 0 ? this.firstValue : sourceIndex >= this.inputSamples ? this.lastValue : this.ring[sourceIndex % this.ring.length];
          value += input * kernel[tap];
        }
      }
      if (this.filled === 0) this.pendingSince = sampleAt;
      this.pending[this.filled++] = value; this.totalSamples++;
      this.levelAccumulator += value * value; this.levelCount++;
      if (this.filled === this.chunkSamples && !this.flush()) return false;
    }
    return true;
  }

  flush() {
    if (this.filled === 0) return true;
    const samples = this.filled, contextTime = this.pendingSince;
    const pcm = new Int16Array(samples);
    for (let i = 0; i < samples; i++) {
      const value = Math.max(-1, Math.min(1, this.pending[i]));
      pcm[i] = Math.round(value * (value < 0 ? 0x8000 : 0x7fff));
    }
    this.frames.push({ total: this.totalSamples, contextTime });
    this.sentSamples = this.totalSamples; this.filled = 0; this.pendingSince = null;
    try {
      this.port.postMessage({ type: 'pcm', buffer: pcm.buffer, samples,
        total_samples: this.sentSamples, context_time: contextTime }, [pcm.buffer]);
      return true;
    } catch { this.fail('AUDIO_INTERRUPTED', '音频传输失败，已停止本轮录音'); return false; }
  }

  complete(limit) {
    if (this.stopped || !this.drain(true) || !this.flush()) return;
    this.stopped = true;
    if (limit) this.port.postMessage({ type: 'limit', total_samples: this.totalSamples });
    this.port.postMessage({ type: 'flushed', total_samples: this.totalSamples });
    this.clear();
  }

  clear() {
    this.pending.fill(0); this.ring.fill(0); this.inputTimes.fill(0);
    this.firstValue = this.lastValue = 0;
    this.filled = 0; this.pendingSince = null; this.frames = [];
    this.levelAccumulator = this.levelCount = 0;
  }

  fail(code, message) {
    if (this.stopped) return;
    this.stopped = this.failed = true;
    this.clear();
    try { this.port.postMessage({ type: 'error', code, message }); }
    catch { /* A dead port cannot safely accept more audio. */ }
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
