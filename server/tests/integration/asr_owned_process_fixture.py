"""Spawn-importable synthetic transcribe only; never imports NumPy/MLX.

The actual DecodeWorker._start/Pipe/_worker_main/stop remain production code.
All files and all OS children belong to the invoking test's nonce and PID.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import time


def bootstrap(connection):
    # Spawn imports a fresh module; the parent's monkeypatch is not inherited.
    from app.services import asr_core
    asr_core.transcribe = synthetic_transcribe
    asr_core._worker_main(connection)


def synthetic_transcribe(pcm, model_dir, language, prompt):
    folder = Path(model_dir).resolve()
    expected = Path(os.environ['SVI_OWNED_ASR_FIXTURE_DIR']).resolve()
    if folder != expected:
        raise RuntimeError('not the process-owned synthetic model directory')
    owner = json.loads((folder / 'owner.json').read_text())
    if os.getppid() != owner['parent_pid'] or len(owner['nonce']) != 32:
        raise RuntimeError('unexpected synthetic process parent')
    if language != 'auto' or prompt != '':
        raise RuntimeError('unexpected synthetic request')

    def record(event, **extra):
        value = {'event': event, 'pid': os.getpid(), 'parent_pid': os.getppid(),
                 'nonce': owner['nonce'], 'monotonic': time.monotonic(), **extra}
        fd = os.open(folder / f'child-{os.getpid()}.jsonl',
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            payload = (json.dumps(value, sort_keys=True) + '\n').encode()
            if os.write(fd, payload) != len(payload):
                raise RuntimeError('incomplete synthetic event record')
        finally:
            os.close(fd)

    if pcm == bytes(32000):
        # Production prepare/_warm uses 1s digital silence; this is not a model.
        record('synthetic_warm_return')
        return {'words': [], 'segments': [], 'seconds': 0}
    if pcm != (100).to_bytes(2, 'little', signed=True) * 4000:
        raise RuntimeError('unexpected synthetic PCM; no real audio is accepted')

    def ignore_term(signum, frame):
        record('sigterm_ignored', signal=signum)

    signal.signal(signal.SIGTERM, ignore_term)
    record('synthetic_decode_enter')
    safety_deadline = time.monotonic() + 45
    while os.getppid() == owner['parent_pid'] and time.monotonic() < safety_deadline:
        time.sleep(.05)
    # Test safety only; a safety exit cannot satisfy the required SIGKILL oracle.
    os._exit(89)
