"""Preparation checks; synthetic metadata never certifies capacity."""
import hashlib
import json
from pathlib import Path
import runpy

import pytest
from app.core.config import Config

TOOL_PATH = Path(__file__).resolve().parents[3] / 'scripts/acceptance/capacity_resource_snapshot.py'
T = runpy.run_path(str(TOOL_PATH))


def tool_function(name):
    return T[name]


def test_weight_digest_uses_same_priority_and_bytes_as_loader(tmp_path):
    (tmp_path/'config.json').write_bytes(b'configuration')
    (tmp_path/'weights.npz').write_bytes(b'unselected')
    (tmp_path/'weights.safetensors').write_bytes(b'selected')
    (tmp_path/'model.safetensors').write_bytes(b'unsupported')
    assert T['mlx_identity'](tmp_path) == {'format':'weights.safetensors', 'sha256':hashlib.sha256(b'configurationselected').hexdigest()}
    (tmp_path/'weights.safetensors').write_bytes(b'changed')
    assert T['mlx_identity'](tmp_path)['sha256'] == hashlib.sha256(b'configurationchanged').hexdigest()


def test_huggingface_filename_alone_is_not_actual_mlx_model(tmp_path):
    (tmp_path/'config.json').write_bytes(b'{}')
    (tmp_path/'model.safetensors').write_bytes(b'model')
    with pytest.raises(ValueError, match='MLX_MODEL_INCOMPLETE'): T['mlx_identity'](tmp_path)


def test_limit_and_report_path_are_not_new_resource_identity():
    a = Config(); b = a.model_copy(deep=True)
    b.sessions.initial_max_active = 10
    b.capacity.validation_report_path = '/private/new-report.json'
    assert T['configuration_identity'](a) == T['configuration_identity'](b)


@pytest.mark.parametrize('field', ['execution_slots', 'decode_timeout_ms'])
def test_actual_execution_resource_change_changes_identity(field):
    a = Config(); b = a.model_copy(deep=True)
    setattr(b.asr, field, getattr(a.asr, field)+1)
    assert T['configuration_identity'](a) != T['configuration_identity'](b)


@pytest.mark.parametrize('url', ['http://localhost:11434','https://127.0.0.1:11434','http://192.168.1.1:11434',
                               'http://user:secret@127.0.0.1:11434','http://127.0.0.1:11434/?token=secret',
                               'http://127.0.0.1:11434/private'])
def test_no_connection_for_unapproved_metadata_destination(url, monkeypatch):
    fn = T['get_local_json']
    monkeypatch.setitem(fn.__globals__, 'http', type('NoHTTP', (), {'client':None}))
    with pytest.raises(ValueError, match='NUMERIC_LOOPBACK_ONLY'): fn(url, '/api/version')


def test_probe_keeps_only_requested_model_and_rejects_cloud(monkeypatch):
    fn = T['ollama_identity']; requests = []; remote = False
    def get(base, path):
        requests.append(path)
        if path == '/api/version': return {'version':'test-version'}
        return {'models':[{'name':'private-other-model', 'digest':'2'*64},
                          {'name':'target', 'digest':'1'*64, 'remote_host':'cloud.invalid' if remote else ''}]}
    monkeypatch.setitem(fn.__globals__, 'get_local_json', get)
    result = fn('http://127.0.0.1:11434', 'target')
    assert requests == ['/api/version','/api/tags']
    assert 'private-other-model' not in json.dumps(result)
    assert result == {'model':'target','digest':'1'*64,'ollama_version':'test-version'}
    remote = True
    with pytest.raises(ValueError, match='OLLAMA_REMOTE_MODEL'): fn('http://127.0.0.1:11434', 'target')


def test_default_snapshot_never_calls_network_or_certifies_runtime(tmp_path, monkeypatch):
    fn = T['collect']
    (tmp_path/'server/app').mkdir(parents=True)
    (tmp_path/'server/app/example.py').write_text('x = 1\n')
    monkeypatch.setitem(fn.__globals__, 'hardware', lambda:{'gpu':[{'sppci_model':'fake'}], 'ram_bytes':1})
    monkeypatch.setitem(fn.__globals__, 'ollama_identity', lambda *a:pytest.fail('network not opted in'))
    cfg = Config(); cfg._jwt_secret='never-output-this-secret';cfg.asr.model_path='/private/never-output-this-path'
    r=fn(tmp_path,cfg)
    s=json.dumps(r)
    assert r['capacity_validation']=='unverified' and r['acceptance_pass_claimed'] is False
    assert 'environment_fingerprint' not in s and 'normal_completed' not in s
    assert 'never-output-this' not in s
    assert 'TRUSTED_RESOURCE_PROBE' in r['missing'] and 'EFFECTIVE_RUNTIME_CONFIG_AND_QUOTAS' in r['missing']
    assert r['snapshot_sha256']==T['digest'](r['material'])
    (tmp_path/'server/app/example.py').write_text('x = 2\n')
    assert fn(tmp_path,cfg)['snapshot_sha256'] != r['snapshot_sha256']


def test_existing_output_never_overwritten(tmp_path, capsys):
    config=tmp_path/'config.yaml';config.write_text('{}')
    output=tmp_path/'existing.json';output.write_text('keep-original')
    assert T['main'](['--config',str(config),'--out',str(output)])==1
    assert output.read_text()=='keep-original'
    assert 'FileExistsError' in capsys.readouterr().out
