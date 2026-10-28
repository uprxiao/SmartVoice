"""Causal segment cutter for cloud ASR (V2 design §3.2; experiment batches 05/09).

Each complete 250 ms block is judged as it arrives: once the open segment is
at least ``max(min_s, target_s - window_s)`` long, the first quiet block ends
the segment; at ``target_s + window_s`` it is cut regardless. Audio is never
held back and no future audio is consulted.
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16_000
BLOCK_SAMPLES = 4_000  # 250 ms
BASELINE_SAMPLES = 20 * SAMPLE_RATE
VOICED_RMS = 200.0
FALLBACK_SILENCE_RMS = 150.0


def block_rms(pcm_s16le: bytes) -> float:
    samples = np.frombuffer(pcm_s16le, dtype='<i2').astype(np.float64)
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


class Segmenter:
    def __init__(self, *, target_s: float, window_s: float, min_s: float,
                 silence_ratio: float, start_sample: int = 0):
        self.earliest = int(max(min_s, target_s - window_s) * SAMPLE_RATE)
        self.latest = int((target_s + window_s) * SAMPLE_RATE)
        self.silence_ratio = silence_ratio
        self.origin = start_sample
        self.segment_start = start_sample
        self.position = start_sample  # samples judged so far (block aligned)
        self._partial = bytearray()
        self._voiced: list[float] = []
        # Whether the open segment has any voiced block, and the flag of each cut
        # segment (cut position -> voiced). Used to discard filler that the cloud
        # model emits for pure silence (real-cloud finding, 2026-10-28).
        self.segment_voiced = False
        self.voiced_by_cut: dict[int, bool] = {}

    def threshold(self) -> float:
        if not self._voiced:
            return FALLBACK_SILENCE_RMS
        return self.silence_ratio * float(np.median(self._voiced))

    def push(self, pcm_s16le: bytes) -> list[int]:
        """Consume PCM; return the sample positions where segments were cut."""
        self._partial.extend(pcm_s16le)
        cuts: list[int] = []
        block_bytes = BLOCK_SAMPLES * 2
        while len(self._partial) >= block_bytes:
            block = bytes(self._partial[:block_bytes])
            del self._partial[:block_bytes]
            rms = block_rms(block)
            if rms > VOICED_RMS:
                self.segment_voiced = True
                if self.position - self.origin < BASELINE_SAMPLES:
                    self._voiced.append(rms)
            self.position += BLOCK_SAMPLES
            length = self.position - self.segment_start
            if length >= self.latest or (length >= self.earliest and rms < self.threshold()):
                cuts.append(self.position)
                self.voiced_by_cut[self.position] = self.segment_voiced
                self.segment_voiced = False
                self.segment_start = self.position
        return cuts

    def tail_voiced(self) -> bool:
        """Voiced flag of the open segment including the unjudged partial block."""
        return self.segment_voiced or block_rms(bytes(self._partial)) > VOICED_RMS
