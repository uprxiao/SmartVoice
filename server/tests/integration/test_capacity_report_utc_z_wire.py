"""Serializer control only: synthetic validated summaries are never capacity attestations."""
from __future__ import annotations

from copy import deepcopy
import datetime as dt
import hashlib
import json

import pytest

from app.api import admin as admin_api
from app.services.capacity_report import validate_summary
from tests.conftest import auth_headers
from tests.test_admin import login_admin
from tests.unit.test_capacity_report import summary


@pytest.mark.parametrize('status,fingerprint', [('valid', '1' * 64), ('stale', '9' * 64)], ids=['synthetic-valid', 'synthetic-stale'])
def test_validated_summary_tested_at_wire_is_utc_z_without_attesting(
    app_client, monkeypatch, tmp_path, record_property, status, fingerprint
):
    report = summary()
    report.update(report_id='fixture-only-wire-not-attestation', tested_at='2026-01-01T00:00:00.123456+00:00')
    encoded = json.dumps(report, sort_keys=True).encode()
    original_file = tmp_path / 'synthetic-wire-source-not-deployment-report.json'
    original_file.write_bytes(encoded)
    original_sha = hashlib.sha256(encoded).hexdigest()
    # This is the existing pure validator fixture, not a deployed runtime identity.
    value = validate_summary(report, fingerprint)
    assert value['status'] == status and value['report'] is report
    before = deepcopy(value)
    assert getattr(app_client.app.state, 'trusted_environment_fingerprint', None) is None
    calls = []

    def validated_summary_port(path, actual_runtime_fingerprint):
        calls.append({'path_is_unset': path is None, 'runtime_fingerprint_is_none': actual_runtime_fingerprint is None})
        assert actual_runtime_fingerprint is None
        return value

    # Only the already-validated result port is controlled. No report is installed,
    # no trusted application state is assigned, and the real HTTP/DB route runs.
    monkeypatch.setattr(admin_api, 'load_capacity_report', validated_summary_port)
    headers = auth_headers(login_admin(app_client))
    first = app_client.get('/api/admin/capacity', headers=headers)
    assert first.status_code == 200
    body = first.json()
    patched = app_client.patch('/api/admin/capacity', headers=headers,
        json={'version': body['config']['version'], 'max_active_sessions': 10})
    assert patched.status_code == 200 and patched.json()['config']['max_active_sessions'] == 10
    observations = []
    for label, wire in [('get', body), ('patch', patched.json())]:
        actual = wire['validation']
        assert actual['status'] == status
        assert set(actual['report']) == set(report)
        assert {k: v for k, v in actual['report'].items() if k != 'tested_at'} == {
            k: v for k, v in report.items() if k != 'tested_at'}
        tested = actual['report']['tested_at']
        assert dt.datetime.fromisoformat(tested.replace('Z', '+00:00')) == dt.datetime.fromisoformat(report['tested_at'])
        observations.append({'method': label, 'status': status, 'tested_at': tested})
    assert value == before and value['report'] is report
    assert original_file.read_bytes() == encoded and hashlib.sha256(original_file.read_bytes()).hexdigest() == original_sha
    assert len(calls) == 2 and all(c['runtime_fingerprint_is_none'] for c in calls)
    assert getattr(app_client.app.state, 'trusted_environment_fingerprint', None) is None
    record_property('capacity_report_utc_wire', json.dumps({
        'synthetic_validated_summary_port': True, 'production_report_installed': False,
        'trusted_runtime_identity_assigned': False, 'CAP17_pass_claimed': False,
        'real_capacity_or_throughput_attested': False, 'original_source_sha256': original_sha,
        'original_bytes_and_object_unchanged': True, 'observed': observations}, sort_keys=True))
    assert all(row['tested_at'] == '2026-01-01T00:00:00.123456Z' for row in observations)
