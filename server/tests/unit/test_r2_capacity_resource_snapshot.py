"""R2 preparation identities only; no model loading or capacity attestation."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import sys

import pytest
from app.core.config import Config, LlmConfig

TOOL_PATH = Path(__file__).resolve().parents[3]/'scripts/acceptance/capacity_resource_snapshot.py'
T = runpy.run_path(str(TOOL_PATH))


def qwen_files(tmp_path):
    root = tmp_path/'qwen'
    root.mkdir()
    (root/'config.json').write_text('{"model_type":"synthetic-qwen"}')
    (root/'model.safetensors').write_bytes(b'synthetic-model-not-loadable')
    files = [dict(file=path.name, bytes=path.stat().st_size,
                  sha256=hashlib.sha256(path.read_bytes()).hexdigest()) for path in sorted(root.iterdir())]
    manifest = tmp_path/'qwen-manifest.json'
    manifest.write_text(json.dumps(dict(schema='svi-qwen3-asr-manifest-v1', files=files)))
    return root, manifest, hashlib.sha256(manifest.read_bytes()).hexdigest()


def runtime_files(tmp_path):
    root = tmp_path/'runtime'
    package = root/'mlx_qwen3_asr'
    metadata = root/'mlx_qwen3_asr-0.4.4.dist-info'
    package.mkdir(parents=True)
    metadata.mkdir()
    (package/'__init__.py').write_text('raise RuntimeError("this module must never execute")\n')
    (metadata/'METADATA').write_text('Metadata-Version: 2.1\nName: mlx-qwen3-asr\nVersion: 0.4.4\n')
    return root


def virtual_repo(tmp_path):
    root = tmp_path/'repo'
    (root/'server/app').mkdir(parents=True)
    (root/'server/app/example.py').write_text('x=1\n')
    return root


def r2_configuration(tmp_path):
    root, manifest, _ = qwen_files(tmp_path)
    runtime = runtime_files(tmp_path)
    repo = virtual_repo(tmp_path)
    config = Config()
    config.asr.provider = 'qwen3_mlx'
    config.asr.model_id = 'qwen3-asr-1.7b'
    config.asr.model_path = str(root)
    config.asr.model_manifest_path = str(manifest)
    config.asr.runtime_path = str(runtime)
    config.llm = LlmConfig(provider='openai_compatible', model_id='deepseek-flash',
                           base_url='https://api.deepseek.com', thinking='disabled')
    config.classification_llm = LlmConfig(provider='ollama', model_id='qwen3-coder:30b',
                                         base_url='http://127.0.0.1:11434')
    return config, repo, root, manifest, runtime


def test_r2_qwen_complete_model_manifest_identity(tmp_path):
    root, manifest, digest = qwen_files(tmp_path)
    value = T['qwen_identity'](root, manifest, digest, 'qwen3-asr-1.7b')
    assert value == dict(provider='qwen3_mlx', model_id='qwen3-asr-1.7b@'+digest,
                         manifest_sha256=digest, loader_file_count=2)


def test_r2_qwen_and_independent_classification_both_have_material(tmp_path, monkeypatch):
    root, manifest, _ = qwen_files(tmp_path)
    runtime = runtime_files(tmp_path)
    repo = virtual_repo(tmp_path)
    config = Config()
    config.asr.provider = 'qwen3_mlx'
    config.asr.model_id = 'qwen3-asr-1.7b'
    config.asr.model_path = str(root)
    config.asr.model_manifest_path = str(manifest)
    config.asr.runtime_path = str(runtime)
    config.llm = LlmConfig(provider='openai_compatible', model_id='deepseek-flash',
                           base_url='https://api.deepseek.com', thinking='disabled')
    config.classification_llm = LlmConfig(provider='ollama', model_id='qwen3-coder:30b',
                                         base_url='http://127.0.0.1:11434')
    collect = T['collect']
    calls = []
    monkeypatch.setitem(collect.__globals__, 'hardware', lambda:dict(gpu=[{'sppci_model':'synthetic'}],ram_bytes=1))
    def metadata(url, model):
        calls.append((url, model))
        return dict(model=model, digest='1'*64, ollama_version='synthetic')
    monkeypatch.setitem(collect.__globals__, 'ollama_identity', metadata)
    value = collect(repo, config, probe_local_ollama=True)
    assert value['material']['models']['asr']['provider'] == 'qwen3_mlx'
    assert value['material']['models']['classification_llm']['model'] == 'qwen3-coder:30b'
    assert calls == [('http://127.0.0.1:11434', 'qwen3-coder:30b')]
    assert value['capacity_validation'] == 'unverified' and not value['acceptance_pass_claimed']
    assert 'environment_fingerprint' not in json.dumps(value)


def test_r2_runtime_metadata_source_digest_without_import(tmp_path):
    root = runtime_files(tmp_path)
    before = set(sys.modules)
    value = T['qwen_runtime_identity'](str(root))
    assert value['distribution'] == 'mlx-qwen3-asr' and value['version'] == '0.4.4'
    assert value['python_source_files'] == 1
    assert len(value['python_sources_sha256']) == 64
    assert not any(name == module or name.startswith(module+'.')
                   for name in set(sys.modules)-before for module in ('mlx_qwen3_asr','mlx','torch'))
    assert 'metadata_source_only_no_process_attestation' == value['origin']


@pytest.mark.parametrize('mode', ['missing', 'expected-sha', 'changed-weight', 'extra-file',
    'missing-weight', 'wrong-size', 'escaped-weight', 'fifo', 'schema-list', 'missing-key'])
def test_r2_invalid_model_material_never_gets_identity(tmp_path, mode):
    root, manifest, _ = qwen_files(tmp_path)
    expected = ''
    if mode == 'missing':manifest.unlink()
    elif mode == 'expected-sha':expected = '0'*64
    elif mode == 'changed-weight':(root/'model.safetensors').write_bytes(b'different')
    elif mode == 'extra-file':(root/'tokenizer_config.json').write_text('{}')
    elif mode == 'missing-weight':(root/'model.safetensors').unlink()
    elif mode == 'wrong-size':
        value = json.loads(manifest.read_text());value['files'][1]['bytes'] += 1
        manifest.write_text(json.dumps(value))
    elif mode == 'escaped-weight':
        other = tmp_path/'outside.safetensors';other.write_bytes((root/'model.safetensors').read_bytes())
        (root/'model.safetensors').unlink();(root/'model.safetensors').symlink_to(other)
    elif mode == 'fifo':manifest.unlink();os.mkfifo(manifest)
    elif mode == 'schema-list':manifest.write_text('[]')
    elif mode == 'missing-key':
        value = json.loads(manifest.read_text());value['files'][0].pop('sha256')
        manifest.write_text(json.dumps(value))
    with pytest.raises((ValueError, OSError)):
        T['qwen_identity'](root, manifest, expected)


@pytest.mark.parametrize('mode', ['missing-dist', 'wrong-version', 'duplicate-dist', 'source-missing',
                                  'escaped-source', 'fifo-source', 'missing-name'])
def test_r2_invalid_runtime_source_never_gets_identity(tmp_path, mode):
    root = runtime_files(tmp_path)
    metadata = root/'mlx_qwen3_asr-0.4.4.dist-info/METADATA'
    package = root/'mlx_qwen3_asr'
    if mode == 'missing-dist':metadata.unlink()
    elif mode == 'wrong-version':metadata.write_text('Name: mlx-qwen3-asr\nVersion: 0.4.3\n')
    elif mode == 'duplicate-dist':
        extra = root/'duplicate.dist-info';extra.mkdir();(extra/'METADATA').write_bytes(metadata.read_bytes())
    elif mode == 'source-missing':(package/'__init__.py').unlink()
    elif mode == 'escaped-source':
        outside = tmp_path/'outside.py';outside.write_text('private_source=1\n')
        (package/'__init__.py').unlink();(package/'__init__.py').symlink_to(outside)
    elif mode == 'fifo-source':(package/'__init__.py').unlink();os.mkfifo(package/'__init__.py')
    elif mode == 'missing-name':metadata.write_text('Version: 0.4.4\n')
    with pytest.raises((ValueError, OSError)):
        T['qwen_runtime_identity'](str(root))


def test_r2_runtime_source_change_changes_digest(tmp_path):
    root = runtime_files(tmp_path)
    before = T['qwen_runtime_identity'](str(root))
    (root/'mlx_qwen3_asr/__init__.py').write_text('raise RuntimeError("changed, still must not execute")\n')
    after = T['qwen_runtime_identity'](str(root))
    assert before['python_sources_sha256'] != after['python_sources_sha256']


def test_r2_default_is_offline_and_does_not_expose_private_paths_or_keys(tmp_path, monkeypatch):
    config, repo, *_ = r2_configuration(tmp_path)
    config._llm_api_key = 'never-output-r2-public-key'
    config._classification_llm_api_key = 'never-output-r2-local-key'
    collect = T['collect']
    monkeypatch.setitem(collect.__globals__, 'hardware', lambda:dict(gpu=[{}],ram_bytes=1))
    def forbidden(*args):pytest.fail('metadata network was not opted in')
    monkeypatch.setitem(collect.__globals__, 'ollama_identity', forbidden)
    monkeypatch.setitem(collect.__globals__, 'get_local_json', forbidden)
    value = collect(repo, config)
    text = json.dumps(value)
    assert value['material']['models']['asr']['provider'] == 'qwen3_mlx'
    assert 'ACTUAL_LOCAL_CLASSIFICATION_MODEL' in value['missing']
    assert 'ACTUAL_PUBLIC_LLM_IDENTITY_AND_QUOTAS' in value['missing']
    assert str(tmp_path) not in text and 'never-output-r2-' not in text
    assert value['capacity_validation'] == 'unverified' and value['acceptance_pass_claimed'] is False
    assert 'trusted_environment_fingerprint' not in text


def test_r2_relative_model_manifest_runtime_use_production_server_base(tmp_path, monkeypatch):
    config, repo, model, manifest, runtime = r2_configuration(tmp_path)
    shutil.copytree(model, repo/'server/model')
    shutil.copytree(runtime, repo/'server/runtime')
    (repo/'server/config').mkdir()
    (repo/'server/config/qwen.json').write_bytes(manifest.read_bytes())
    config.asr.model_path = 'model'
    config.asr.model_manifest_path = 'config/qwen.json'
    config.asr.runtime_path = 'runtime'
    collect = T['collect']
    monkeypatch.setitem(collect.__globals__, 'hardware', lambda:dict(gpu=[{}],ram_bytes=1))
    value = collect(repo, config)
    assert value['material']['models']['asr']['provider'] == 'qwen3_mlx'
    assert value['material']['models']['asr_runtime']['version'] == '0.4.4'


def test_r2_cli_snapshot_file_is_private_even_under_permissive_umask(tmp_path, monkeypatch, capsys):
    config = tmp_path/'config.yaml';config.write_text('{}')
    out = tmp_path/'private-snapshot.json'
    monkeypatch.setitem(T['collect'].__globals__, 'hardware', lambda:dict(gpu=[{}],ram_bytes=1))
    previous_mask = os.umask(0)
    try:
        assert T['main'](['--config',str(config),'--out',str(out)]) == 0
    finally:os.umask(previous_mask)
    assert out.stat().st_mode & 0o777 == 0o600
    assert '"capacity_validation": "unverified"' in capsys.readouterr().out
