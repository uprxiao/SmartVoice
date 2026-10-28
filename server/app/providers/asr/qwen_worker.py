"""Isolated native Qwen sessions; imported by parent without loading MLX."""
from __future__ import annotations

import os
import sys

from .qwen_checkpoint import SCHEMA, clear as clear_checkpoint, export as export_checkpoint, restore as restore_checkpoint


def _clear(state, np):
    # Drop session-owned acoustic arrays/caches. Shared model weights remain.
    state.buffer = np.array([], dtype=np.float32)
    state.audio_accum = np.array([], dtype=np.float32)
    state._prefix_cache = None
    state._window_raw_ids = []
    state.context = state.text = state.stable_text = state.committed_text = state.window_text = ''


def qwen_worker_main(connection):
    states = {}
    model = np = None
    try:
        while True:
            message = connection.recv()
            if message is None:
                return
            pcm, command = message
            value = None
            try:
                action = command['action']
                if action == 'prepare':
                    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
                    if command.get('runtime_path'):
                        sys.path.insert(0, command['runtime_path'])
                    from importlib.metadata import version
                    if version('mlx-qwen3-asr') != '0.4.4':
                        raise RuntimeError('unsupported_qwen_runtime')
                    import numpy as np
                    from mlx_qwen3_asr import Session
                    from threading import RLock
                    from tqdm import tqdm
                    tqdm.set_lock(RLock())
                    model = Session(model=command['model_path'])
                    warm = model.init_streaming(language='Chinese', chunk_size_sec=2,
                        max_context_sec=30, max_new_tokens=4096)
                    warm = model.feed_audio(np.zeros(32000, dtype=np.float32), warm)
                    warm = model.finish_streaming(warm)
                    _clear(warm, np)
                    warm = None
                    value = {'ready': True, 'runtime': 'mlx-qwen3-asr/0.4.4', 'mlx': version('mlx'),
                        'checkpoint_schema': SCHEMA}
                elif action == 'drop':
                    item = states.pop(tuple(command['owner']), None)
                    if item is not None:
                        _clear(item['state'], np)
                    item = None
                    value = {'dropped': True}
                elif action in {'feed', 'finish'}:
                    if model is None:
                        raise RuntimeError('qwen_not_prepared')
                    owner = tuple(command['owner'])
                    item = states.get(owner)
                    if item is None:
                        checkpoint = command.get('checkpoint')
                        if checkpoint is not None:
                            item = restore_checkpoint(checkpoint, owner, command['start_sample'], model, np)
                            clear_checkpoint(checkpoint)
                        else:
                            if command['start_sample'] != 0:
                                raise RuntimeError('qwen_session_state_lost')
                            options = command['options']
                            state = model.init_streaming(context=options['context'], language=options['language'],
                                chunk_size_sec=2, max_context_sec=30, max_new_tokens=4096)
                            item = {'state': state, 'samples': 0, 'nonzero': False}
                        states[owner] = item
                    elif command.get('checkpoint') is not None:
                        raise ValueError('qwen_live_state_cannot_be_overwritten')
                    if item['samples'] != command['start_sample']:
                        raise ValueError('qwen_audio_not_contiguous')
                    audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
                    item['nonzero'] = item['nonzero'] or bool(np.any(audio))
                    state = model.feed_audio(audio, item['state'])
                    item['samples'] += len(audio)
                    if action == 'finish':
                        state = model.finish_streaming(state)
                    if len(state._window_raw_ids) >= 4096:
                        raise RuntimeError('qwen_token_cap')
                    item['state'] = state
                    # v0.4.4's heuristic stable_text can rewrite its prefix.
                    # Only flushed windows are immutable; keep the full live
                    # text visible as provisional without changing recognition.
                    stable = state.text if action == 'finish' else state.committed_text
                    value = {'text': state.text if item['nonzero'] else '',
                        'stable': stable if item['nonzero'] else '', 'samples': item['samples']}
                    if action == 'finish':
                        states.pop(owner, None)
                        _clear(state, np)
                    else:
                        value['checkpoint'] = export_checkpoint(item, owner)
                    audio = state = item = None
                else:
                    raise ValueError('unsupported_qwen_command')
                connection.send((True, value))
            except BaseException as exc:
                # Only a fixed error type crosses the telemetry boundary. A
                # failed generation is killed by DecodeWorker before reuse.
                connection.send((False, type(exc).__name__))
            finally:
                if isinstance(command, dict):
                    clear_checkpoint(command.get('checkpoint'))
                if isinstance(value, dict):
                    clear_checkpoint(value.get('checkpoint'))
                pcm = message = command = value = None
    except (EOFError, BrokenPipeError):
        pass
    finally:
        states.clear()
        connection.close()
