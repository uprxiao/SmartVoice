"""ADMIN-07: every real admin operation rejects valid but unauthorized tokens.

Uses the existing process-owned PostgreSQL fixture and real authentication.
No dependency overrides, forged JWTs, disabled limiter or permission stubs.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.routing import iter_route_contexts

from tests.conftest import auth_headers, execute_sql, register_and_login
from tests.cases.test_dict_cases import fetch_one
from tests.test_admin import login_admin


DAY = "2026-10-16"
ADMIN_OPERATIONS = (
    ("GET", "/api/admin/users"),
    ("POST", "/api/admin/users"),
    ("PATCH", "/api/admin/users/{user_id}"),
    ("GET", "/api/admin/stats/overview"),
    ("GET", "/api/admin/stats/usage"),
    ("GET", "/api/admin/stats/models"),
    ("GET", "/api/admin/capacity"),
    ("PATCH", "/api/admin/capacity"),
    # V2 provider management (design §8.1); fictional development mock rows only.
    ("GET", "/api/admin/providers"),
    ("POST", "/api/admin/providers"),
    ("GET", "/api/admin/providers/active"),
    ("PUT", "/api/admin/providers/active"),
    ("GET", "/api/admin/providers/audit"),
    ("PATCH", "/api/admin/providers/{provider_id}"),
    ("POST", "/api/admin/providers/{provider_id}/test"),
    ("DELETE", "/api/admin/providers/{provider_id}"),
)


def assert_complete_admin_inventory(app):
    # FastAPI 0.143.3 keeps include_router branches lazy. The supported iterator
    # expands their effective paths, including routes hidden from OpenAPI.
    actual = set()
    for route in iter_route_contexts(app.routes):
        if route.path and (route.path == "/api/admin" or route.path.startswith("/api/admin/")):
            assert route.methods, f"uncovered non-HTTP admin route: {route.path}"
            actual.update((method, route.path) for method in route.methods)
    expected = set(ADMIN_OPERATIONS)
    assert actual == expected, {"missing_cases": sorted(actual - expected), "stale_cases": sorted(expected - actual)}


@pytest.fixture()
def admin07_accounts(app_client):
    admin_tokens = login_admin(app_client)
    admin = auth_headers(admin_tokens)
    ordinary_tokens = register_and_login(app_client, email="admin07-ordinary@example.test")
    created = app_client.post(
        "/api/admin/users",
        headers={**admin, "Idempotency-Key": str(uuid.uuid4())},
        json={"email": "admin07-target@example.test", "password": "TargetPass123"},
    )
    assert created.status_code == 201, created.text
    target = created.json()["user"]
    target_login = app_client.post(
        "/api/auth/login",
        json={
            "email": target["email"], "password": "TargetPass123", "client_type": "desktop",
            "device": {"installation_id": str(uuid.uuid4()), "name": "admin07-fixture",
                       "platform": "macos", "client_version": "0.0.1"},
        },
    )
    assert target_login.status_code == 200, target_login.text
    assert target["role"] == "user" and target["status"] == "active"
    providers = {}
    for name in ("admin07-edit", "admin07-activate"):
        made = app_client.post("/api/admin/providers", headers=admin, json={
            "kind": "llm", "name": name, "type": "mock", "model_id": "mock-llm", "params": {}})
        assert made.status_code == 201, made.text
        providers[name] = made.json()["id"]
    assert app_client.get("/api/admin/providers/active", headers=admin).status_code == 200
    return {"admin_tokens": admin_tokens, "ordinary_tokens": ordinary_tokens, "target_id": target["id"],
            "provider_id": providers["admin07-edit"], "activate_id": providers["admin07-activate"]}


def valid_request(app_client, operation, target_id, provider_ids=None):
    method, template = operation
    provider_ids = provider_ids or {}
    path = template.format(user_id=target_id, provider_id=provider_ids.get("provider_id"))
    kwargs = {}
    if template.startswith("/api/admin/stats/"):
        kwargs["params"] = {"date_from": DAY, "date_to": DAY}
    elif method == "GET" and template == "/api/admin/users":
        kwargs["params"] = {"limit": 20, "status": "active"}
    elif method == "POST" and template == "/api/admin/users":
        kwargs["headers"] = {"Idempotency-Key": str(uuid.uuid4())}
        kwargs["json"] = {"email": f"admin07-denied-{uuid.uuid4().hex}@example.test",
                          "password": "MemberPass123", "display_name": "Authorization fixture"}
    elif method == "PATCH" and template == "/api/admin/users/{user_id}":
        # Real, active, different ordinary user: never a nonexistent target,
        # the requesting user, or the protected last administrator.
        kwargs["json"] = {"status": "disabled"}
    elif method == "PATCH" and template == "/api/admin/capacity":
        current = fetch_one("select max_active_sessions from runtime_capacity where id=1")
        version = fetch_one("select version from runtime_capacity where id=1")
        proposed = 1 if current > 1 else 2
        assert proposed <= app_client.app.state.config.effective_admission_ceiling
        assert proposed != current
        kwargs["json"] = {"version": version, "max_active_sessions": proposed}
    elif method == "POST" and template == "/api/admin/providers":
        kwargs["json"] = {"kind": "llm", "name": f"admin07-{uuid.uuid4().hex[:8]}", "type": "mock",
                          "model_id": "mock-llm", "params": {}}
    elif method == "PUT" and template == "/api/admin/providers/active":
        version = fetch_one("select version from provider_active where kind='llm'")
        kwargs["json"] = {"kind": "llm", "provider_id": provider_ids.get("activate_id"), "version": version}
    elif method == "PATCH" and template == "/api/admin/providers/{provider_id}":
        version = fetch_one("select version from provider_configs where id=$1", provider_ids.get("provider_id"))
        kwargs["json"] = {"version": version, "name": f"admin07-renamed-{uuid.uuid4().hex[:6]}"}
    return method, path, kwargs


def request_as(app_client, tokens, request_spec):
    method, path, kwargs = request_spec
    supplied = dict(kwargs)
    headers = {**auth_headers(tokens), **supplied.pop("headers", {})}
    return app_client.request(method, path, headers=headers, **supplied)


def protected_state(app_client):
    # Hash whole synthetic business rows rather than duplicating their evolving
    # schemas or printing password hashes / idempotency hashes. Ordered JSONB
    # snapshots detect inserts, deletes and updates, not merely row counts.
    # authenticate legitimately updates last_used_at, so login revocation is
    # checked explicitly while that one access timestamp is excluded.
    raw = fetch_one("""
        select jsonb_build_object(
          'users', (select coalesce(jsonb_agg(jsonb_build_object('id', t.id,
              'row', md5(to_jsonb(t)::text)) order by t.id), '[]'::jsonb) from users t),
          'dictionary_state', (select coalesce(jsonb_agg(jsonb_build_object('id', t.user_id,
              'row', md5(to_jsonb(t)::text)) order by t.user_id), '[]'::jsonb) from dictionary_state t),
          'runtime_capacity', (select coalesce(jsonb_agg(jsonb_build_object('id', t.id,
              'row', md5(to_jsonb(t)::text)) order by t.id), '[]'::jsonb) from runtime_capacity t),
          'capacity_changes', (select coalesce(jsonb_agg(jsonb_build_object('id', t.id,
              'row', md5(to_jsonb(t)::text)) order by t.id), '[]'::jsonb) from capacity_changes t),
          'operation_requests', (select coalesce(jsonb_agg(jsonb_build_object('id', t.id,
              'row', md5(to_jsonb(t)::text)) order by t.id), '[]'::jsonb) from operation_requests t),
          'login_sessions', (select coalesce(jsonb_agg(jsonb_build_object('id', t.id,
              'row', md5((to_jsonb(t) - 'last_used_at')::text)) order by t.id), '[]'::jsonb) from login_sessions t),
          'provider_configs', (select coalesce(jsonb_agg(jsonb_build_object('id', t.id,
              'row', md5(to_jsonb(t)::text)) order by t.id), '[]'::jsonb) from provider_configs t),
          'provider_active', (select coalesce(jsonb_agg(jsonb_build_object('id', t.kind,
              'row', md5(to_jsonb(t)::text)) order by t.kind), '[]'::jsonb) from provider_active t),
          'provider_audit', (select coalesce(jsonb_agg(jsonb_build_object('id', t.id,
              'row', md5(to_jsonb(t)::text)) order by t.id), '[]'::jsonb) from provider_audit t)
        )
    """)
    return {"database": json.loads(raw), "capacity_cache": app_client.app.state.capacity._cached}


@pytest.mark.parametrize("operation", ADMIN_OPERATIONS, ids=[f"{method} {path}" for method, path in ADMIN_OPERATIONS])
@pytest.mark.parametrize("actor_kind", ["ordinary", "revoked_admin"])
def test_tc_admin_07_every_operation_forbidden_without_business_writes(app_client, admin07_accounts, actor_kind, operation):
    assert_complete_admin_inventory(app_client.app)
    accounts = admin07_accounts
    if actor_kind == "revoked_admin":
        tokens = accounts["admin_tokens"]
        assert tokens["user"]["role"] == "admin"
        allowed = app_client.get("/api/admin/users", headers=auth_headers(tokens))
        assert allowed.status_code == 200, allowed.text
        # Simulated operator action changes only the current DB role. Keep the
        # very same, still-valid pre-revocation token and login session.
        execute_sql("update users set role='user' where id=$1", tokens["user"]["id"])
    else:
        tokens = accounts["ordinary_tokens"]
    current = app_client.get("/api/users/me", headers=auth_headers(tokens))
    assert current.status_code == 200, current.text
    assert current.json()["user"]["role"] == "user"
    assert current.json()["user"]["status"] == "active"
    request_spec = valid_request(app_client, operation, accounts["target_id"], accounts)
    before = protected_state(app_client)

    response = request_as(app_client, tokens, request_spec)

    assert protected_state(app_client) == before, operation
    assert response.status_code == 403, (operation, response.status_code, response.text)
    assert response.json()["error"]["code"] == "FORBIDDEN", operation


def test_admin07_matrix_payloads_succeed_for_an_active_admin(app_client, admin07_accounts):
    """Positive control: invalid JSON/path/CAS must not create a false denial test."""
    assert_complete_admin_inventory(app_client.app)
    tokens, target_id = admin07_accounts["admin_tokens"], admin07_accounts["target_id"]
    for operation in ADMIN_OPERATIONS:
        request_spec = valid_request(app_client, operation, target_id, admin07_accounts)
        response = request_as(app_client, tokens, request_spec)
        expected = 201 if operation in (("POST", "/api/admin/users"), ("POST", "/api/admin/providers")) else 200
        assert response.status_code == expected, (operation, response.status_code, response.text)
        if operation == ("POST", "/api/admin/users"):
            user_id = response.json()["user"]["id"]
            assert fetch_one("select role='user' and status='active' from users where id=$1", user_id) is True
            assert fetch_one("select version from dictionary_state where user_id=$1", user_id) == 0
        elif operation == ("PATCH", "/api/admin/users/{user_id}"):
            assert fetch_one("select status from users where id=$1", target_id) == "disabled"
            assert fetch_one("select count(*) from login_sessions where user_id=$1 and revoked_at is null", target_id) == 0
        elif operation == ("PATCH", "/api/admin/capacity"):
            body = request_spec[2]["json"]
            assert fetch_one("select max_active_sessions from runtime_capacity where id=1") == body["max_active_sessions"]
            assert fetch_one("select version from runtime_capacity where id=1") == body["version"] + 1
            assert fetch_one("select count(*) from capacity_changes where to_version=$1 and actor_user_id=$2",
                             body["version"] + 1, tokens["user"]["id"]) == 1
