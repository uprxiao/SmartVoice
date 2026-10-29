"""TC-WIN-20/34/35: real PostgreSQL + HTTP/WS, synthetic model/audio fixtures only."""
from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import auth_headers, register_and_login
from tests.test_admin import login_admin
from tests.test_insertions_learning import claim, receipt
from tests.test_sessions import dictation


@pytest.mark.parametrize("status,observable,code", [
    ("inserted", True, None),
    ("failed", False, "PERMISSION_DENIED"),
    ("unverified", False, "VERIFY_FAILED"),
])
def test_windows_receipt_keeps_learning_account_preference_and_statistics_truthful(app_client, status, observable, code):
    register_and_login(app_client, email="windows@example.test")
    logged_in = app_client.post("/api/auth/login", json={
        "email": "windows@example.test", "password": "SmokePass123", "client_type": "desktop",
        "device": {"installation_id": str(uuid.uuid4()), "name": "Windows fictional fixture",
                   "platform": "windows", "client_version": "0.0.2"},
    })
    assert logged_in.status_code == 200, logged_in.text
    headers = auth_headers(logged_in.json())
    assert app_client.get("/api/v1/settings", headers=headers).json()["learning_enabled"] is True
    request_id = str(uuid.uuid4())
    created = app_client.post("/api/v1/sessions", headers={**headers, "Idempotency-Key": request_id},
                              json={"client_request_id": request_id, "learning_enabled": False})
    assert created.status_code == 201, created.text
    session = created.json()
    assert session["learning_enabled"] is False
    result = dictation(app_client, headers, session["session_id"])[-1]["result"]
    insertion_id = str(uuid.uuid4())
    assert claim(app_client, headers, session["session_id"], result, insertion_id).status_code == 201
    assert claim(app_client, headers, session["session_id"], result, insertion_id).status_code == 200
    for _ in range(2):
        response = receipt(app_client, headers, session["session_id"], insertion_id, status, observable, code)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == status
        assert response.json()["observable"] is observable
    saved = app_client.get(f"/api/v1/sessions/{session['session_id']}", headers=headers).json()
    assert saved["learning_enabled"] is False
    assert saved["insertion_status"] == status
    assert app_client.get("/api/v1/settings", headers=headers).json()["learning_enabled"] is True
    day = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    params = {"date_from": day, "date_to": day}
    totals = app_client.get("/api/v1/stats/me", headers=headers, params=params).json()["totals"]
    assert totals["sessions"] == 1
    assert totals["inserted_sessions"] == int(status == "inserted")
    assert totals["insertion_success_rate"] == float(status == "inserted")
    assert totals["learning_events"] == 0
    assert app_client.get("/api/admin/stats/overview", headers=headers, params=params).status_code == 403
    other = auth_headers(register_and_login(app_client, email="other@example.test"))
    assert app_client.get(f"/api/v1/sessions/{session['session_id']}", headers=other).status_code == 404
    assert receipt(app_client, other, session["session_id"], insertion_id, status, observable, code).status_code == 404
    admin = auth_headers(login_admin(app_client))
    overview = app_client.get("/api/admin/stats/overview", headers=admin, params=params)
    assert overview.status_code == 200, overview.text
    assert overview.json()["totals"]["sessions"] == 1
    assert overview.json()["totals"]["inserted_sessions"] == int(status == "inserted")
