"""Read-only capacity preparation; never a runtime attestation or CapacityReport."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit

MAX_JSON = 2_097_152
HEX64 = re.compile(r'[0-9a-f]{64}\Z')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path, initial=b''):
    h = hashlib.sha256(initial)
    # Explicit model directories may contain symlinks managed by a local cache;
    # only regular opened files are read, never devices or FIFO peers.
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('NOT_REGULAR_FILE')
        for chunk in iter(lambda: stream.read(1_048_576), b''):
            h.update(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError('FILE_CHANGED')
    return h.hexdigest()


def mlx_identity(folder):
    folder = Path(folder)
    weights = next((folder / n for n in ('weights.safetensors', 'weights.npz') if (folder / n).is_file()), None)
    if weights is None or not (folder / 'config.json').is_file():
        raise ValueError('MLX_MODEL_INCOMPLETE')
    # The production loader hashes these same bytes in this same order.
    with (folder / 'config.json').open('rb') as stream:
        config = stream.read(MAX_JSON + 1)
    if len(config) > MAX_JSON:
        raise ValueError('MODEL_CONFIG_TOO_LARGE')
    return {'format': weights.name, 'sha256': file_digest(weights, config)}


def small_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('INPUT_NOT_REGULAR_OR_TOO_LARGE')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (len(raw) > limit or (before.st_size, before.st_mtime_ns, before.st_ino)
                != (after.st_size, after.st_mtime_ns, after.st_ino)):
            raise ValueError('INPUT_CHANGED')
        return raw


def qwen_identity(folder, manifest_path, expected_sha='', model_id='qwen3-asr-1.7b'):
    """Use the production loader's exact offline file-set/content checks."""
    if not manifest_path:
        raise ValueError('QWEN_MODEL_MANIFEST_REQUIRED')
    from app.providers.asr.qwen_manifest import validate_model_manifest
    raw = small_regular(manifest_path, 65_536)
    document = json.loads(raw)
    if (not isinstance(document, dict) or not isinstance(document.get('files'), list)
            or not document['files'] or any(not isinstance(item, dict)
                or not {'file', 'bytes', 'sha256'} <= set(item) for item in document['files'])):
        raise ValueError('QWEN_MANIFEST_STRUCTURE')
    identity = validate_model_manifest(folder, manifest_path, expected_sha)
    if identity != hashlib.sha256(raw).hexdigest() or small_regular(manifest_path, 65_536) != raw:
        raise ValueError('QWEN_MANIFEST_CHANGED')
    return {'provider': 'qwen3_mlx', 'model_id': (model_id or 'qwen3-asr-1.7b')+'@'+identity,
            'manifest_sha256': identity, 'loader_file_count': len(document['files'])}


def qwen_runtime_identity(runtime_path=None):
    """Read installed metadata/source, without importing or executing MLX/Qwen."""
    if runtime_path:
        root = Path(runtime_path).resolve(strict=True)
        distributions = list(importlib.metadata.distributions(path=[str(root)]))
    else:
        distributions = [importlib.metadata.distribution('mlx-qwen3-asr')]
    matches = [d for d in distributions
               if str(d.metadata.get('Name', '')).lower().replace('_', '-') == 'mlx-qwen3-asr']
    if len(matches) != 1 or matches[0].version != '0.4.4':
        raise ValueError('QWEN_RUNTIME_MISSING_DUPLICATE_OR_UNSUPPORTED')
    package = Path(matches[0].locate_file('mlx_qwen3_asr')).resolve(strict=True)
    if not package.is_dir():
        raise ValueError('QWEN_RUNTIME_SOURCE_MISSING')
    sources = {}
    for path in sorted(package.rglob('*.py')):
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(package):
            raise ValueError('QWEN_RUNTIME_SOURCE_ESCAPED')
        sources[str(path.relative_to(package))] = file_digest(resolved)
    if '__init__.py' not in sources:
        raise ValueError('QWEN_RUNTIME_SOURCE_MISSING')
    return {'distribution': 'mlx-qwen3-asr', 'version': matches[0].version,
            'python_source_files': len(sources), 'python_sources_sha256': digest(sources),
            'origin': 'metadata_source_only_no_process_attestation'}


def get_local_json(base_url, path):
    u = urlsplit(base_url)
    if (u.scheme != 'http' or u.hostname != '127.0.0.1' or u.username is not None
            or u.password is not None or u.path not in ('', '/') or u.query or u.fragment):
        raise ValueError('NUMERIC_LOOPBACK_ONLY')
    port = u.port or 80
    if not 1 <= port <= 65535:
        raise ValueError('LOOPBACK_PORT')
    # HTTPConnection ignores proxy environment variables and does not redirect.
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        connection.request('GET', path, headers={'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('METADATA_STATUS')
        data = response.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise ValueError('METADATA_TOO_LARGE')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('METADATA_SCHEMA')
        return value
    finally:
        connection.close()


def ollama_identity(base_url, model):
    version = get_local_json(base_url, '/api/version').get('version')
    tags = get_local_json(base_url, '/api/tags').get('models')
    if not isinstance(version, str) or not version or not isinstance(tags, list):
        raise ValueError('OLLAMA_METADATA_MISSING')
    matches = [m for m in tags if isinstance(m, dict) and m.get('name') == model]
    if len(matches) != 1:
        raise ValueError('OLLAMA_TARGET_MISSING')
    target = matches[0]
    if target.get('remote_host') or target.get('remote_model'):
        raise ValueError('OLLAMA_REMOTE_MODEL')
    if not isinstance(target.get('digest'), str) or not HEX64.fullmatch(target['digest']):
        raise ValueError('OLLAMA_DIGEST_MISSING')
    return {'model': model, 'digest': target['digest'], 'ollama_version': version}


def _command(args):
    try:
        return subprocess.check_output(args, timeout=5, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def hardware():
    value = {'system': platform.system(), 'release': platform.release(), 'version': platform.version(),
             'architecture': platform.machine(), 'logical_cpus': os.cpu_count()}
    if value['system'] == 'Darwin':
        for key, name in [('ram_bytes', 'hw.memsize'), ('physical_cpus', 'hw.physicalcpu'), ('cpu_name', 'machdep.cpu.brand_string')]:
            v = _command(['/usr/sbin/sysctl', '-n', name])
            value[key] = int(v) if key != 'cpu_name' and v and v.isdigit() else v
        raw = _command(['/usr/sbin/system_profiler', 'SPDisplaysDataType', '-json'])
        try:
            cards = json.loads(raw)['SPDisplaysDataType']
            keys = ('sppci_model', 'spdisplays_cores', 'spdisplays_vendor', 'spdisplays_metal')
            value['gpu'] = [{k: x[k] for k in keys if k in x} for x in cards]
        except (TypeError, ValueError, KeyError):
            value['gpu'] = None
    else:
        value['gpu'] = None
        value['ram_bytes'] = None
    return value


def configuration_identity(config):
    value = config.model_dump()
    value.pop('capacity', None)
    value['sessions'].pop('initial_max_active', None)
    # Paths are represented by actual model bytes separately; credentials and
    # environment variables are never fetched by this YAML-only preparation.
    value['asr'].pop('model_path', None)
    return digest(value)


def collect(repo, config, *, probe_local_ollama=False):
    source_files = sorted((repo / 'server/app').rglob('*.py'))
    source_files += [p for p in (repo/'server/pyproject.toml', repo/'server/uv.lock') if p.is_file()]
    source = {str(p.relative_to(repo)): file_digest(p) for p in source_files}
    workload = {str(p.relative_to(repo)): file_digest(p) for p in sorted((repo/'scripts/acceptance').glob('*.py'))}
    versions = {}
    for name in ('fastapi', 'uvicorn', 'asyncpg', 'mlx', 'mlx-whisper', 'mlx-qwen3-asr', 'numpy', 'tqdm'):
        try: versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: versions[name] = None
    h = hardware()
    missing = ['EFFECTIVE_RUNTIME_CONFIG_AND_QUOTAS', 'TRUSTED_RESOURCE_PROBE', 'REAL_CAPACITY_RAW_EVIDENCE', 'GPU_DRIVER_AND_CONTAINER_QUOTAS']
    if not h.get('gpu') or not h.get('ram_bytes'):
        missing.append('HARDWARE_GPU_OR_RAM')
    models = {}
    if config.asr.provider == 'mlx_whisper':
        try: models['asr'] = mlx_identity(config.asr.model_path)
        except (OSError, ValueError): missing.append('ACTUAL_ASR_MODEL_BYTES')
    elif config.asr.provider == 'qwen3_mlx':
        def server_path(value):
            return repo/'server'/value if value and not Path(value).is_absolute() else value
        manifest_path = config.asr.model_manifest_path
        try:
            models['asr'] = qwen_identity(server_path(config.asr.model_path), server_path(manifest_path),
                                          config.asr.model_sha256, config.asr.model_id)
        except (OSError, ValueError, TypeError):
            missing.append('ACTUAL_QWEN_ASR_MODEL_BYTES')
        try:
            models['asr_runtime'] = qwen_runtime_identity(server_path(config.asr.runtime_path))
        except (OSError, ValueError, TypeError, importlib.metadata.PackageNotFoundError):
            missing.append('ACTUAL_QWEN_NATIVE_RUNTIME_SOURCE')
    else: missing.append('ACTUAL_LOCAL_MLX_ASR')
    if config.llm.provider == 'ollama' and probe_local_ollama:
        try: models['llm'] = ollama_identity(config.llm.base_url, config.llm.model_id)
        except (OSError, ValueError, http.client.HTTPException): missing.append('ACTUAL_LOCAL_OLLAMA_MODEL')
    elif config.llm.provider == 'openai_compatible':
        # Configuration is not the public service's live model/quota identity.
        # Never send an automatic public probe or read an API key here.
        models['llm_configuration'] = {'provider': 'openai_compatible', 'model': config.llm.model_id,
                                       'origin': 'configuration_only_no_public_probe'}
        missing.append('ACTUAL_PUBLIC_LLM_IDENTITY_AND_QUOTAS')
    else: missing.append('ACTUAL_LOCAL_OLLAMA_MODEL')
    classification = config.classification_llm
    if classification is not None:
        if classification.provider == 'ollama' and probe_local_ollama:
            try:
                models['classification_llm'] = ollama_identity(classification.base_url, classification.model_id)
            except (OSError, ValueError, http.client.HTTPException):
                missing.append('ACTUAL_LOCAL_CLASSIFICATION_MODEL')
        else:
            missing.append('ACTUAL_LOCAL_CLASSIFICATION_MODEL')
    material = {'hardware': h, 'python': platform.python_version(), 'packages': versions,
                'source_sha256': digest(source), 'workload_sha256': digest(workload),
                'yaml_configuration_sha256': configuration_identity(config), 'models': models}
    return {'schema': 'svi-capacity-resource-snapshot-v1', 'material': material,
            'snapshot_sha256': digest(material), 'configuration_origin': 'yaml_only_no_env_overlay',
            'missing': missing, 'capacity_validation': 'unverified', 'acceptance_pass_claimed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--probe-local-ollama', action='store_true')
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo/'server'))
    from app.core.config import Config
    import yaml
    try:
        if Path(args.out).exists() or Path(args.out).is_symlink():
            raise FileExistsError('OUTPUT_EXISTS')
        fd = os.open(args.config, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('CONFIG_NOT_REGULAR')
            raw = stream.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ValueError('CONFIG_TOO_LARGE')
        config = Config.model_validate(yaml.safe_load(raw) or {})
        result = collect(repo, config, probe_local_ollama=args.probe_local_ollama)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    except Exception as exc:
        # Config validation can include sensitive values or private paths.
        print(json.dumps({'status': 'not_created', 'error_type': type(exc).__name__}))
        return 1
    print(json.dumps({'status': 'snapshot_created', 'capacity_validation': 'unverified',
                      'missing_count': len(result['missing']), 'acceptance_pass_claimed': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
