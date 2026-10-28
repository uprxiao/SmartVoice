"""Actual worker entry, fictional native Session; no MLX/model load."""
import copy
import importlib.metadata
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from app.providers.asr.qwen_worker import qwen_worker_main
from tests.qwen_native_fixtures import native_state


@pytest.mark.parametrize('nonzero', [True, False])
def test_worker_exposes_only_committed_text_as_stable_and_preserves_the_live_body(monkeypatch, nonzero):
    live_calls = []
    reports = [
        ('临时候选甲。', '临时候选', ''),
        ('改写候选乙。', '上次误标稳定的字', ''),
        ('已提交段。候选丙。', '已提交段。候选', '已提交段。'),
        ('已提交段。候选丁。', '已提交段。候选丁', '已提交段。'),
    ]
    stream_meta = {}
    class Session:
        def __init__(self, model):
            assert model == 'fictional-model'
        def init_streaming(self, **kwargs):
            assert kwargs['chunk_size_sec'] == 2 and kwargs['max_context_sec'] == 30
            assert kwargs['max_new_tokens'] == 4096
            state = native_state(context=kwargs.get('context', ''), language=kwargs.get('language'))
            stream_meta[id(state)] = dict(warm='context' not in kwargs, index=0)
            return state
        def feed_audio(self, audio, state):
            meta = stream_meta[id(state)]
            if not meta['warm']:
                assert len(audio) in (32000, 123)
                live_calls.append(len(audio))
                state.text, state.stable_text, state.committed_text = reports[meta['index']]
                meta['index'] += 1
            return state
        def finish_streaming(self, state):
            if not stream_meta[id(state)]['warm']:
                state.text = state.stable_text = '已提交段。最终丁。'
            return state
    monkeypatch.setitem(sys.modules, 'mlx_qwen3_asr', SimpleNamespace(Session=Session))
    monkeypatch.setattr(importlib.metadata, 'version', lambda name: '0.4.4' if name == 'mlx-qwen3-asr' else 'fictional')
    owner = ('fictional-user', 'fictional-session', 1)
    pcm = (b'\x01\x00' if nonzero else b'\x00\x00')
    messages = [(b'', {'action': 'prepare', 'model_path': 'fictional-model'})]
    for i in range(3):
        messages.append((pcm*32000, {'action': 'feed', 'owner': owner, 'start_sample': i*32000,
            'options': {'context': 'React', 'language': 'Chinese'}}))
    messages.append((pcm*123, {'action': 'finish', 'owner': owner, 'start_sample': 96000,
        'options': {'context': 'React', 'language': 'Chinese'}}))
    messages.append(None)
    class Connection:
        def __init__(self):self.sent=[];self.closed=False
        def recv(self):return messages.pop(0)
        def send(self, value):self.sent.append(copy.deepcopy(value))
        def close(self):self.closed=True
    c = Connection()
    from tqdm import tqdm
    original_lock = tqdm.get_lock()
    try:
        qwen_worker_main(c)
    finally:
        tqdm.set_lock(original_lock)
    assert c.closed and len(c.sent) == 5 and all(ok for ok, _ in c.sent)
    values = [v for _, v in c.sent[1:]]
    assert live_calls == [32000, 32000, 32000, 123]
    assert [v['samples'] for v in values] == [32000, 64000, 96000, 96123]
    if nonzero:
        assert [v['stable'] for v in values] == ['', '', '已提交段。', '已提交段。最终丁。']
        assert [v['text'] for v in values] == [reports[0][0], reports[1][0], reports[2][0], '已提交段。最终丁。']
        stable = ''
        for v in values:
            assert v['stable'].startswith(stable) and v['text'].startswith(v['stable'])
            stable = v['stable']
    else:
        assert all(v['text'] == v['stable'] == '' for v in values)
