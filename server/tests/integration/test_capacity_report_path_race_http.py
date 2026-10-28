"""Real admin/PG reads under owned filesystem races; never inject identity."""
import json
import os
from pathlib import Path

import pytest

from app.services import capacity_report
from tests.conftest import auth_headers
from tests.test_admin import login_admin
from tests.unit.test_capacity_report import summary


@pytest.mark.parametrize("swap_at", ["root", "parent", "leaf"])
def test_admin_report_read_never_opens_outside_after_parent_swap(
    app_client, tmp_path, monkeypatch, record_property, swap_at,
):
    headers = auth_headers(login_admin(app_client))
    baseline = app_client.get("/api/admin/capacity", headers=headers)
    assert baseline.status_code == 200
    assert app_client.app.state.trusted_environment_fingerprint is None
    server = tmp_path / "owned-server"
    root = server / "config"
    parent = root / "nested"
    parent.mkdir(parents=True)
    report = parent / "report.json"
    report.write_text(json.dumps(summary() | {"report_id": "owned-inside"}))
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    payload = json.dumps(summary() | {"report_id": "outside-not-allowed"})
    (outside / "report.json").write_text(payload)
    (outside / "nested/report.json").write_text(payload)
    outside_files = {(p.stat().st_dev, p.stat().st_ino) for p in outside.rglob("*.json")}
    observation = {"swapped": False, "outside_opened": False}

    class ObservedOS:
        def __getattr__(self, name):
            return getattr(os, name)

        def open(self, path, flags, *args, **kwargs):
            target = {"root": root.name, "parent": parent.name, "leaf": report.name}[swap_at]
            if not observation["swapped"] and (Path(path) == report or Path(path).name == target):
                observation["swapped"] = True
                moved = root if swap_at == "root" else parent
                moved.rename(moved.with_name(moved.name + "-held"))
                moved.symlink_to(outside, target_is_directory=True)
            fd = os.open(path, flags, *args, **kwargs)
            opened = os.fstat(fd)
            observation["outside_opened"] |= (opened.st_dev, opened.st_ino) in outside_files
            return fd

    monkeypatch.setattr(capacity_report, "__file__", str(server / "app/services/capacity_report.py"))
    monkeypatch.setattr(capacity_report, "os", ObservedOS())
    monkeypatch.setattr(app_client.app.state.config.capacity, "validation_report_path", str(report))
    response = app_client.get("/api/admin/capacity", headers=headers)
    assert response.status_code == 200
    assert response.json()["validation"] == {"status": "unverified", "report": None}
    assert response.json()["config"] == baseline.json()["config"]
    assert app_client.app.state.trusted_environment_fingerprint is None
    assert observation == {"swapped": True, "outside_opened": False}
    health = app_client.get("/healthz")
    assert health.status_code == 200 and health.json()["ready"] is True
    record_property("capacity_report_directory_swap", json.dumps({
        "swap_at": swap_at, **observation, "http_status": response.status_code,
        "validation": response.json()["validation"], "configuration_unchanged": True,
        "trusted_identity_assigned": False, "CAP17_pass_claimed": False,
    }, sort_keys=True))
