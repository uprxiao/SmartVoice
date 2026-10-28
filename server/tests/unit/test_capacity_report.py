"""Validate report gates; fabricated fixtures are not performance evidence."""
import json
import pytest
from app.services.capacity_report import validate_summary, load_capacity_report


def summary():
    return dict(report_id='fixture-only', tested_at='2026-01-01T00:00:00Z', target_sessions=20,
        environment_fingerprint='1' * 64, app_commit='2' * 40, config_sha256='3' * 64,
        fixture_manifest_sha256='4' * 64, hardware_model_manifest_sha256='5' * 64, evidence_sha256='6' * 64,
        normal_completed=1600, normal_total=1600, rejected=0, failed=0, fallback=0,
        stop_to_result_p50_ms=2000, stop_to_result_p95_ms=5000,
        desktop_stop_to_readback_p50_ms=2000, desktop_stop_to_readback_p95_ms=5000, soak_passed=True)


def test_summary_never_attests_itself():
    assert validate_summary(summary(), None) == {'status': 'unverified', 'report': None}
    assert validate_summary(summary(), '1' * 64)['status'] == 'valid'
    assert validate_summary(summary(), '9' * 64)['status'] == 'stale'


@pytest.mark.parametrize('key,value', [
    ('normal_completed', 1599), ('normal_total', 1599), ('target_sessions', True),
    ('rejected', 1), ('failed', 1), ('fallback', 1), ('soak_passed', False),
    ('stop_to_result_p50_ms', 2001), ('stop_to_result_p95_ms', 5001),
    ('desktop_stop_to_readback_p50_ms', 2001), ('desktop_stop_to_readback_p95_ms', 5001),
    ('app_commit', 'wrong'), ('tested_at', '2999-01-01T00:00:00Z')])
def test_invalid_or_incomplete_reports_cannot_be_valid(key, value):
    data = summary(); data[key] = value
    assert validate_summary(data, '1' * 64) == {'status': 'unverified', 'report': None}


def test_report_path_must_be_bounded_regular_file_in_deployment_root(tmp_path):
    allowed = tmp_path / 'allowed'; allowed.mkdir()
    report = allowed / 'report.json'; report.write_text(json.dumps(summary()))
    assert load_capacity_report(str(report), '1' * 64, allowed_roots=[allowed])['status'] == 'valid'
    link = allowed / 'link.json'; link.symlink_to(report)
    assert load_capacity_report(str(link), '1' * 64, allowed_roots=[allowed])['status'] == 'unverified'
    assert load_capacity_report(str(report), '1' * 64, allowed_roots=[tmp_path / 'other'])['status'] == 'unverified'
    report.write_bytes(b' ' * 65537)
    assert load_capacity_report(str(report), '1' * 64, allowed_roots=[allowed])['status'] == 'unverified'
