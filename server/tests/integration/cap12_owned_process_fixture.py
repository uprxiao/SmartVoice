"""Spawn-only physical stub for CAP12; no model weights/NumPy/MLX import.

Parent retains production DecodeWorker, Pipe, _worker_main, timeout and stop.
Only marker100's first physical call sleeps3s; the production3s total deadline
may terminate it earlier because the reviewed1s metadata reserve is unchanged.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import struct
import time


def bootstrap(connection):
    from app.services import asr_core
    asr_core.transcribe = synthetic_transcribe
    asr_core._worker_main(connection)


def synthetic_transcribe(pcm, model_dir, language, prompt):
    folder = Path(model_dir).resolve()
    if folder != Path(os.environ['SVI_CAP12_OWNED_DIR']).resolve():
        raise RuntimeError('CAP12_MODEL_DIRECTORY')
    owner = json.loads((folder/'owner.json').read_text())
    if os.getppid() != owner['parent_pid'] or len(owner['nonce']) != 32:
        raise RuntimeError('CAP12_OWNED_PARENT')
    if language != 'auto' or not isinstance(prompt,str):
        raise RuntimeError('CAP12_REQUEST_METADATA')

    def record(event, **fields):
        data = {'event':event,'pid':os.getpid(),'parent_pid':os.getppid(),
                'nonce':owner['nonce'],'monotonic':time.monotonic(),**fields}
        fd = os.open(folder/f'child-{os.getpid()}.jsonl',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        try:
            payload=(json.dumps(data,sort_keys=True)+'\n').encode()
            if os.write(fd,payload)!=len(payload): raise RuntimeError('CAP12_EVENT_WRITE')
        finally: os.close(fd)

    if pcm == bytes(32000):
        record('warm_return'); return {'words':[],'segments':[],'seconds':0.0}
    if not pcm or len(pcm)%2:
        raise RuntimeError('CAP12_PCM_SHAPE')
    marker=struct.unpack_from('<h',pcm)[0]
    if not 100<=marker<=119 or pcm!=struct.pack('<h',marker)*(len(pcm)//2):
        raise RuntimeError('CAP12_SYNTHETIC_PCM_ONLY')
    began=time.monotonic(); samples=len(pcm)//2
    record('decode_enter',marker=marker,samples=samples)
    if marker==100:
        try: claimed=os.open(folder/'first-block-claimed',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError: claimed=None
        if claimed is not None:
            os.close(claimed)
            record('three_second_block_enter',marker=marker,configured_seconds=3)
            # Genuine blocking child call; no event-loop sleep/deadline replacement.
            # Default SIGTERM remains active so production stop can confirm death.
            time.sleep(3)
            record('three_second_block_return',marker=marker)
    text=f'容量测试账户{marker}完成。'
    record('decode_return',marker=marker,samples=samples)
    return {'words':[(0.0,samples/16000,text)],'segments':[samples/16000],
            'seconds':time.monotonic()-began}
