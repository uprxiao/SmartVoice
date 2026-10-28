"""Real NumPy/production control flow, with only the MLX model call replaced.

These regressions prove sample preservation and call/error boundaries. They do
not load MLX weights, execute physical inference, assess speech quality, start
worker processes, or validate DB usage records.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from app.providers.asr.mlx_whisper import MlxWhisperProvider
from app.services import asr_core


class ModelStub:
    def __init__(self):
        self.inputs = []
        self.error = None
        self.no_speech_prob = 0.01

    def __call__(self, audio, **_options):
        self.inputs.append(audio.copy())
        if self.error is not None:
            raise self.error
        return {
            "segments": [{
                "no_speech_prob": self.no_speech_prob,
                "end": 0.5,
                "words": [{"start": 0.25, "end": 0.5, "word": "FAKE_MODEL_MARKER"}],
            }],
        }


@pytest.fixture
def model_stub(monkeypatch):
    stub = ModelStub()
    module = ModuleType("mlx_whisper")
    module.transcribe = stub
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)
    # Register restoration even though production transcribe writes os.environ
    # directly. No real model import or process-global env changes survive.
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    return stub


def pcm(samples):
    return np.asarray(samples, dtype="<i2").tobytes()


def assert_input_preserved(stub, samples):
    assert len(stub.inputs) == 1
    assert stub.inputs[0].dtype == np.float32
    expected = np.asarray(samples, dtype=np.float32) / 32768.0
    np.testing.assert_array_equal(stub.inputs[0], expected)


@pytest.mark.parametrize("samples", [
    pytest.param([66] * 8000 + [3277] * 8000, id="quiet-nonzero-prefix"),
    pytest.param([66] * 8000 + [3277] * 1600 + [66] * 8000, id="quiet-nonzero-bookends"),
])
def test_transcribe_preserves_complete_nonzero_sequence_and_original_clock(model_stub, samples):
    result = asr_core.transcribe(pcm(samples), "/fake/model", "zh", "")
    assert_input_preserved(model_stub, samples)
    assert result["words"] == [(0.25, 0.5, "FAKE_MODEL_MARKER")]
    assert result["segments"] == [0.5]


@pytest.mark.parametrize("value", [16, -16], ids=["positive", "negative"])
def test_low_nonzero_amplitude_does_not_discard_model_output(model_stub, value):
    samples = [value] * 16000
    result = asr_core.transcribe(pcm(samples), "/fake/model", "zh", "")
    assert_input_preserved(model_stub, samples)
    # A fake low-no_speech response must not be erased by an amplitude threshold.
    # The marker's survival is data-flow evidence, not proof that PCM is speech.
    assert result["words"] == [(0.25, 0.5, "FAKE_MODEL_MARKER")]


def test_digital_zero_reaches_model_before_output_is_suppressed(model_stub):
    samples = [0] * 16000
    result = asr_core.transcribe(pcm(samples), "/fake/model", "zh", "")
    assert_input_preserved(model_stub, samples)
    assert result["words"] == []
    assert result["segments"] == []


def test_model_no_speech_response_is_still_filtered(model_stub):
    model_stub.no_speech_prob = 0.9
    samples = [16] * 16000
    result = asr_core.transcribe(pcm(samples), "/fake/model", "zh", "")
    assert_input_preserved(model_stub, samples)
    assert result["words"] == []
    assert result["segments"] == []


@pytest.mark.parametrize("value", [0, 16], ids=["digital-zero", "low-nonzero"])
def test_model_error_propagates_even_for_silent_or_quiet_input(model_stub, value):
    error = RuntimeError("intentional fake model failure")
    model_stub.error = error
    samples = [value] * 16000
    with pytest.raises(RuntimeError, match="intentional fake model failure") as caught:
        asr_core.transcribe(pcm(samples), "/fake/model", "zh", "")
    assert caught.value is error
    assert_input_preserved(model_stub, samples)


def test_empty_input_has_no_model_invocation(model_stub):
    result = asr_core.transcribe(b"", "/fake/model", "zh", "")
    assert model_stub.inputs == []
    assert result["words"] == []
    assert result["segments"] == []


class LoopbackWorker:
    """No subprocess: run real transcribe and fake only process availability.

    On failure, match DecodeWorker's unavailable-after-error contract. This does
    not exercise the real multiprocessing lifecycle or physical model loading.
    """
    def __init__(self):
        self.process = None

    async def decode(self, args, _deadline):
        self.process = SimpleNamespace(is_alive=lambda: True)
        try:
            return asr_core.transcribe(*args)
        except BaseException:
            self.process = None
            raise

    async def stop(self):
        self.process = None


def provider_with_loopback_worker(tmp_path):
    config_bytes = b'{"fixture":"not a real model"}'
    weight_bytes = b"NOT_MODEL_WEIGHTS"
    (tmp_path / "config.json").write_bytes(config_bytes)
    (tmp_path / "weights.npz").write_bytes(weight_bytes)
    config = asr_core.AsrConfig(model_dir=tmp_path, language="zh", prewarm=True)
    provider = MlxWhisperProvider(config, model_id="fixture")
    # The actual engine/scheduler stay in use. Their constructor has not started
    # a process/task; replacing this unstarted worker cannot load real MLX.
    provider.engine.workers = [LoopbackWorker()]
    return provider, hashlib.sha256(config_bytes + weight_bytes).hexdigest()


def test_engine_prepare_reaches_model_before_provider_health_becomes_true(model_stub, tmp_path):
    async def run():
        provider, expected_digest = provider_with_loopback_worker(tmp_path)
        try:
            assert await provider.health() is False
            await provider.engine.prepare()
            assert_input_preserved(model_stub, [0] * 16000)
            assert provider.engine.model_id == expected_digest
            assert await provider.health() is True
        finally:
            await provider.close()
    asyncio.run(run())


def test_engine_prepare_cannot_succeed_when_warm_model_call_fails(model_stub, tmp_path):
    error = RuntimeError("intentional warm model failure")
    model_stub.error = error

    async def run():
        provider, _ = provider_with_loopback_worker(tmp_path)
        try:
            with pytest.raises(RuntimeError, match="intentional warm model failure") as caught:
                await provider.engine.prepare()
            assert caught.value is error
            assert_input_preserved(model_stub, [0] * 16000)
            assert await provider.health() is False
        finally:
            await provider.close()
    asyncio.run(run())
