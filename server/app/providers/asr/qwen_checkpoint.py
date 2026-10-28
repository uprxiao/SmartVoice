"""Candidate bounded v0.4.4 streaming checkpoint; no MLX import in parent.

Only acoustic window, token rollback and scalar session state cross the pipe.
Model/tokenizer handles and disposable encoder/KV acceleration caches do not.
"""
from __future__ import annotations

import json

SCHEMA = 'svi-qwen-native-checkpoint-v1'
ARRAY_LIMITS = {'buffer': 31999 * 4, 'audio_accum': 480000 * 4}
STRINGS = ('text', 'committed_text', 'window_text', 'context', 'language', 'stable_text',
           'finalization_mode', 'endpointing_mode', '_pre_finalize_text')
OPTIONAL_STRINGS = ('forced_language', '_window_language')
INTEGERS = ('chunk_id', 'unfixed_chunk_num', 'unfixed_token_num', 'chunk_size_samples',
    'max_context_samples', 'sample_rate', 'max_new_tokens', 'endpoint_lookback_samples',
    'endpoint_frame_samples', 'endpoint_min_chunk_samples', 'commit_lookback_samples',
    'commit_min_silence_samples', '_commit_extra_rollback', '_commit_silence_events',
    '_window_chunk_id', '_text_updates', '_rewrite_events', '_finalization_delta_chars')
BOOLEANS = ('enable_tail_refine', 'reuse_window_prefix', 'commit_at_silence', '_adaptive_token_budget')
IGNORED = ('_prefix_cache', '_model_path', '_model_obj', '_tokenizer', '_dtype', '_resolved_model_path')
FIELDS = set(STRINGS + OPTIONAL_STRINGS + INTEGERS + BOOLEANS + ('_window_raw_ids',))
STATE_FIELDS = FIELDS | set(IGNORED) | set(ARRAY_LIMITS)


def validate(value, owner, samples):
    if not isinstance(value, dict) or set(value) != {'schema', 'owner', 'samples', 'nonzero', 'fields', 'arrays'}:
        raise ValueError('invalid native checkpoint envelope')
    if value['schema'] != SCHEMA or value['owner'] != tuple(owner):
        raise ValueError('native checkpoint identity mismatch')
    if type(samples) is not int or not 0 <= samples <= 2880000 or type(value['samples']) is not int or value['samples'] != samples:
        raise ValueError('native checkpoint sample mismatch')
    if type(value['nonzero']) is not bool:
        raise ValueError('invalid native checkpoint nonzero')
    fields, arrays = value['fields'], value['arrays']
    if not isinstance(fields, dict) or set(fields) != FIELDS or not isinstance(arrays, dict) or set(arrays) != set(ARRAY_LIMITS):
        raise ValueError('native checkpoint fields mismatch')
    for key in STRINGS:
        limit = 2000 if key == 'context' else 32768
        if not isinstance(fields[key], str) or len(fields[key]) > limit:
            raise ValueError('native checkpoint text bound exceeded')
    for key in OPTIONAL_STRINGS:
        if fields[key] is not None and (not isinstance(fields[key], str) or len(fields[key]) > 64):
            raise ValueError('invalid native checkpoint language')
    for key in INTEGERS:
        if type(fields[key]) is not int or abs(fields[key]) > 2880000:
            raise ValueError('invalid native checkpoint counter')
    if any(type(fields[key]) is not bool for key in BOOLEANS):
        raise ValueError('invalid native checkpoint switch')
    ids = fields['_window_raw_ids']
    if not isinstance(ids, list) or len(ids) >= 4096 or any(type(i) is not int or not 0 <= i <= 1000000 for i in ids):
        raise ValueError('invalid native checkpoint token rollback')
    required = dict(chunk_size_samples=32000, max_context_samples=480000, sample_rate=16000,
        max_new_tokens=4096, unfixed_chunk_num=2, unfixed_token_num=5)
    if any(fields[key] != expected for key, expected in required.items()):
        raise ValueError('native checkpoint decoding parameters changed')
    for key, limit in ARRAY_LIMITS.items():
        data = arrays[key]
        if not isinstance(data, bytearray) or len(data) % 4 or len(data) > limit:
            raise ValueError('native checkpoint acoustic bound exceeded')
    if sum(len(data) // 4 for data in arrays.values()) > samples:
        raise ValueError('native checkpoint holds more audio than received')
    if len(json.dumps(fields, ensure_ascii=False).encode()) > 1024 * 1024:
        raise ValueError('native checkpoint metadata bound exceeded')
    return value


def export(item, owner):
    state = item['state']
    if set(vars(state)) != STATE_FIELDS:
        raise ValueError('unsupported native streaming state fields')
    value = dict(schema=SCHEMA, owner=tuple(owner), samples=item['samples'], nonzero=item['nonzero'],
        fields={key: list(getattr(state, key)) if key == '_window_raw_ids' else getattr(state, key)
                for key in FIELDS},
        arrays={key: bytearray(getattr(state, key).astype('<f4', copy=False).tobytes()) for key in ARRAY_LIMITS})
    return validate(value, owner, item['samples'])


def restore(value, owner, samples, model, np):
    validate(value, owner, samples)
    fields = value['fields']
    state = model.init_streaming(context=fields['context'], language=fields['forced_language'],
        chunk_size_sec=2, max_context_sec=30, max_new_tokens=4096)
    if set(vars(state)) != STATE_FIELDS:
        raise ValueError('unsupported native streaming state fields')
    for key, data in value['arrays'].items():
        audio = np.frombuffer(data, dtype='<f4').copy()
        if not bool(np.all(np.isfinite(audio))):
            raise ValueError('invalid native checkpoint acoustic values')
        setattr(state, key, audio)
    for key, data in fields.items():
        setattr(state, key, list(data) if key == '_window_raw_ids' else data)
    return dict(state=state, samples=samples, nonzero=value['nonzero'])


def clear(value):
    if not isinstance(value, dict):
        return
    arrays = value.get('arrays', {})
    if isinstance(arrays, dict):
        for data in arrays.values():
            if isinstance(data, bytearray):
                data.clear()
        arrays.clear()
    fields = value.get('fields', {})
    if isinstance(fields, dict):
        for data in fields.values():
            if isinstance(data, list):
                data.clear()
        fields.clear()
    value.clear()
