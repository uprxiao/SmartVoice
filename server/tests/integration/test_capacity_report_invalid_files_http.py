"""Real admin/PG requests must reject invalid reports without attesting fixtures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading

import pytest

from app.services import capacity_report
from tests.conftest import auth_headers
from tests.test_admin import login_admin
from tests.unit.test_capacity_report import summary


@pytest.mark.parametrize('kind', ['fifo', 'directory', 'invalid-json', 'oversized', 'deep-array-json', 'deep-object-json', 'synthetic-without-identity'])
def test_invalid_report_file_keeps_real_admin_available_and_unverified(
    app_client, monkeypatch, tmp_path, record_property, kind
):
    if kind == 'fifo' and not hasattr(os, 'mkfifo'):
        pytest.skip('POSIX deployment special file')
    headers = auth_headers(login_admin(app_client))
    original = app_client.get('/api/admin/capacity', headers=headers)
    assert original.status_code == 200
    assert original.json()['validation'] == {'status': 'unverified', 'report': None}
    assert app_client.app.state.trusted_environment_fingerprint is None

    # Control only the deployment root location, never the actual loader,
    # current resource identity, file-type result, or validated summary.
    server_root = tmp_path / 'owned-deployment/server'
    root = server_root / 'config'
    root.mkdir(parents=True)
    monkeypatch.setattr(capacity_report, '__file__', str(server_root / 'app/services/capacity_report.py'))
    report = root / 'report.json'
    if kind == 'fifo':
        os.mkfifo(report, 0o600)
    elif kind == 'directory':
        report.mkdir()
    elif kind == 'invalid-json':
        report.write_text('{', encoding='utf-8')
    elif kind == 'deep-array-json':
        report.write_text('[' * 16000 + '0' + ']' * 16000, encoding='utf-8')
    elif kind == 'deep-object-json':
        report.write_text('{"nested":' * 5000 + '0' + '}' * 5000, encoding='utf-8')
    elif kind == 'oversized':
        report.write_bytes(b' ' * 65537)
    else:
        report.write_text(json.dumps(summary()), encoding='utf-8')
    monkeypatch.setattr(app_client.app.state.config.capacity, 'validation_report_path', str(report))

    entered, finished = threading.Event(), threading.Event()
    observation = {'peer_needed': False, 'peer_release_error_type': None, 'opened_nonblocking': []}

    class ObservedOS:
        def __getattr__(self, name):
            return getattr(os, name)

        def open(self, path, flags, *args, **kwargs):
            if Path(path) == report or (kwargs.get('dir_fd') is not None and Path(path) == Path(report.name)):
                observation['opened_nonblocking'].append(bool(flags & getattr(os, 'O_NONBLOCK', 0)))
                entered.set()
            return os.open(path, flags, *args, **kwargs)

    monkeypatch.setattr(capacity_report, 'os', ObservedOS())

    def release_owned_fifo_if_regressed():
        entered.wait()
        if finished.wait(2):
            return
        observation['peer_needed'] = True
        try:
            writer = os.open(report, os.O_WRONLY | os.O_NONBLOCK)
            os.close(writer)
        except OSError as exc:
            observation['peer_release_error_type'] = type(exc).__name__

    watchdog = None
    if kind == 'fifo':
        watchdog = threading.Thread(target=release_owned_fifo_if_regressed)
        watchdog.start()
    try:
        response = app_client.get('/api/admin/capacity', headers=headers)
    finally:
        finished.set()
        entered.set()
        if watchdog is not None:
            watchdog.join(timeout=5)
            assert not watchdog.is_alive()
    assert response.status_code == 200
    assert response.json()['validation'] == {'status': 'unverified', 'report': None}
    assert response.json()['config'] == original.json()['config']
    assert app_client.app.state.trusted_environment_fingerprint is None
    healthy = app_client.get('/healthz')
    assert healthy.status_code == 200 and healthy.json()['ready'] is True
    record_property('invalid_capacity_report_real_http', json.dumps({
        'kind': kind, **observation, 'http_status': response.status_code,
        'validation': response.json()['validation'], 'configuration_unchanged': True,
        'subsequent_health_ready': True, 'deployment_root_location_control_only': True,
        'trusted_identity_assigned': False, 'real_capacity_or_CAP17_pass_claimed': False,
    }, sort_keys=True))
    assert observation['peer_release_error_type'] is None
    assert observation['opened_nonblocking'] == [True]
    assert not observation['peer_needed'], 'real admin request blocked until the owned FIFO writer released it'
