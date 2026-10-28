"""Causal segmenter (V2 design §3.2) — pure sample-position checks."""
from app.providers.asr.bailian_segmenter import BLOCK_SAMPLES, Segmenter
from tests.bailian_audio import SAMPLE_RATE, synth


def cuts_for(pcm: bytes, chunk: int = BLOCK_SAMPLES, **kw) -> list[int]:
    seg = Segmenter(target_s=kw.get('target', 30), window_s=kw.get('window', 5), min_s=kw.get('min_s', 10),
                    silence_ratio=0.15)
    cuts = []
    for offset in range(0, len(pcm) // 2, chunk):
        cuts += seg.push(pcm[offset * 2:(offset + chunk) * 2])
    return cuts


def test_first_quiet_block_after_target_minus_window():
    pcm = synth(75, ((27.0, 0.6), (33.0, 0.6), (58.0, 0.6)))
    assert cuts_for(pcm) == [int(27.25 * SAMPLE_RATE), int(58.25 * SAMPLE_RATE)]


def test_hard_cut_at_target_plus_window_without_silence():
    assert cuts_for(synth(100)) == [35 * SAMPLE_RATE, 70 * SAMPLE_RATE]


def test_early_silences_do_not_cut_before_minimum():
    pcm = synth(40, ((3.0, 0.6), (8.0, 0.6), (26.0, 0.6)))
    assert cuts_for(pcm) == [int(26.25 * SAMPLE_RATE)]


def test_odd_chunk_sizes_give_identical_cuts():
    pcm = synth(75, ((27.0, 0.6), (58.0, 0.6)))
    assert cuts_for(pcm, chunk=1_337) == cuts_for(pcm)


def test_no_voice_baseline_uses_fixed_threshold():
    # Quiet constant signal (RMS ~100 < 150) with no voiced block: every block is
    # "quiet", so the first cut lands exactly at the earliest allowed point.
    seg = Segmenter(target_s=30, window_s=5, min_s=10, silence_ratio=0.15)
    assert seg.threshold() == 150.0
    pcm = synth(30, amplitude=140)
    cuts = []
    for offset in range(0, len(pcm) // 2, BLOCK_SAMPLES):
        cuts += seg.push(pcm[offset * 2:(offset + BLOCK_SAMPLES) * 2])
    assert cuts == [25 * SAMPLE_RATE]


def test_reconnect_backoff_spans_short_outages():
    from app.providers.asr.bailian_realtime import reconnect_delay
    delays = [reconnect_delay(n) for n in range(1, 9)]
    assert delays == [0.25, 0.5, 1.0, 2.0, 4.0, 4.0, 4.0, 4.0]
    assert sum(delays) >= 15  # an outage of several seconds no longer exhausts 8 attempts
