"""Fictional state ABI for deterministic worker transport tests; no MLX."""
from types import SimpleNamespace

import numpy as np

from app.providers.asr.qwen_checkpoint import BOOLEANS, INTEGERS, OPTIONAL_STRINGS, STRINGS, export


def native_state(context='', language='Chinese'):
    fields = {key: '' for key in STRINGS}
    fields.update({key: language for key in OPTIONAL_STRINGS})
    fields.update({key: 0 for key in INTEGERS})
    fields.update({key: True for key in BOOLEANS})
    fields.update(context=context, language=language or 'unknown', chunk_size_samples=32000,
        max_context_samples=480000, sample_rate=16000, max_new_tokens=4096,
        unfixed_chunk_num=2, unfixed_token_num=5, finalization_mode='accuracy', endpointing_mode='fixed',
        endpoint_lookback_samples=4800, endpoint_frame_samples=320, endpoint_min_chunk_samples=8000,
        commit_lookback_samples=32000, commit_min_silence_samples=1920, _adaptive_token_budget=False,
        buffer=np.array([], dtype=np.float32), audio_accum=np.array([], dtype=np.float32),
        _window_raw_ids=[], _prefix_cache=None, _model_path='fictional', _model_obj=None,
        _tokenizer=None, _dtype=None, _resolved_model_path=None)
    return SimpleNamespace(**fields)


def native_checkpoint(owner, samples, text, stable, context=''):
    state = native_state(context=context)
    state.text, state.stable_text, state.committed_text = text, stable, stable
    return export(dict(state=state, samples=samples, nonzero=True), owner)
