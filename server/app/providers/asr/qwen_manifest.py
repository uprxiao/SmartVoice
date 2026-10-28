"""Offline content identity for exactly the files the Qwen loader can read."""
import hashlib
import json
from pathlib import Path
import re
import stat


def validate_model_manifest(model_path, manifest_path, expected_sha=''):
    root = Path(model_path).resolve(strict=True)
    mpath = Path(manifest_path)
    info = mpath.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise ValueError('ASR manifest too large')
    with mpath.open('rb') as f:
        raw = f.read(65537)
    if len(raw)>65536:
        raise ValueError('ASR manifest too large')
    identity = hashlib.sha256(raw).hexdigest()
    if expected_sha and identity != expected_sha:
        raise ValueError('ASR manifest SHA mismatch')
    manifest = json.loads(raw)
    if manifest.get('schema') != 'svi-qwen3-asr-manifest-v1' or not manifest.get('files'):
        raise ValueError('invalid Qwen model manifest')
    names = set()
    for item in manifest['files']:
        name = item['file']
        if not isinstance(name, str) or Path(name).name != name or name in {'.','..'} or name in names:
            raise ValueError('invalid Qwen manifest path')
        names.add(name)
        p = (root / name).resolve(strict=True)
        if p.parent != root or not p.is_file() or type(item['bytes']) is not int or p.stat().st_size != item['bytes']:
            raise ValueError('Qwen file missing, escaped or wrong size')
        if not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}',item['sha256']):
            raise ValueError('invalid Qwen file digest')
        h = hashlib.sha256()
        with p.open('rb') as f:
            for block in iter(lambda:f.read(8*1024*1024),b''):
                h.update(block)
        if h.hexdigest() != item['sha256']:
            raise ValueError('Qwen file SHA mismatch')
    loadable = {p.name for p in root.iterdir() if p.suffix in {'.json','.safetensors','.npz','.model','.txt'}}
    if loadable != names or 'config.json' not in names:
        raise ValueError('Qwen loader files do not match manifest')
    return identity
