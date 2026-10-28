from __future__ import annotations

from tests.ids import fixture_uuid

import asyncio
import json
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    PG_HOST,
    PG_PASSWORD,
    PG_PORT,
    PG_USER,
    SERVER_ROOT,
    TEST_DB,
    auth_headers,
    execute_sql,
    register_and_login,
)
from tests.test_sessions import create_session, dictation

PASSWORD = "Password123"


def key() -> str:
    return str(uuid.uuid4())


def fetch_one(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            database=TEST_DB,
        )
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def fetch_all(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            database=TEST_DB,
        )
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def login(client: TestClient, email: str, installation: str = "dict-device"):
    return client.post(
        "/api/auth/login",
        json={
            "email": email,
            "password": PASSWORD,
            "client_type": "desktop",
            "device": {
                "installation_id": fixture_uuid(installation),
                "name": "dict-mac",
                "platform": "macos",
                "client_version": "0.0.1",
            },
        },
    )


def new_user(app_client, email: str, installation: str = "dict-device"):
    assert app_client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    ).status_code == 201
    return login(app_client, email, installation).json()


def create_term(client, headers, term, aliases=None, idem=None):
    return client.post(
        "/api/v1/dictionary/terms",
        json={"term": term, "aliases": aliases or [], "scope": "default"},
        headers={**headers, "Idempotency-Key": idem or key()},
    )


def list_terms(client, headers, **params):
    return client.get("/api/v1/dictionary/terms", params=params, headers=headers)


def test_tc_dict_01_create_and_isolated_version(app_client):
    u1 = new_user(app_client, "dict01-u1@example.com", "dict01-d1")
    u2 = new_user(app_client, "dict01-u2@example.com", "dict01-d2")
    created = create_term(app_client, auth_headers(u1), "Kubernetes", ["k8s"])
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["source"] == "manual"
    assert body["version"] == 1
    assert body["dictionary_version"] == 1
    assert body["aliases"] == ["k8s"]
    u1_list = list_terms(app_client, auth_headers(u1)).json()
    assert u1_list["dictionary_version"] == 1
    u2_list = list_terms(app_client, auth_headers(u2)).json()
    assert u2_list["dictionary_version"] == 0
    assert u2_list["items"] == []


def test_tc_dict_02_alias_removal_clears_bound_contexts(app_client):
    u1 = new_user(app_client, "dict02@example.com", "dict02-d1")
    headers = auth_headers(u1)
    term = create_term(app_client, headers, "Kubernetes", ["k8s", "kube"]).json()
    contexts = [
        {"alias_norm": "k8s", "before": "用", "after": "部署"},
        {"alias_norm": "kube", "before": "查看", "after": "集群"},
    ]
    execute_sql(
        "update dictionary_terms set contexts=$1::jsonb where id=$2",
        json.dumps(contexts),
        term["id"],
    )
    updated = app_client.patch(
        f"/api/v1/dictionary/terms/{term['id']}",
        json={"version": 1, "aliases": ["k8s"]},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["version"] == 2
    assert body["source"] == "manual"
    assert body["dictionary_version"] == 2
    assert [c["alias_norm"] for c in body["contexts"]] == ["k8s"]


def test_tc_dict_03_delete_removes_from_list_and_prompts(app_client):
    u1 = new_user(app_client, "dict03@example.com", "dict03-d1")
    headers = auth_headers(u1)
    term = create_term(app_client, headers, "Kubernetes", ["k8s"]).json()
    deleted = app_client.delete(
        f"/api/v1/dictionary/terms/{term['id']}",
        params={"version": 1},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert deleted.status_code == 200, deleted.text
    listed = list_terms(app_client, headers).json()
    assert listed["items"] == []
    assert listed["dictionary_version"] == 2
    session = create_session(app_client, headers, "dict03-session").json()
    assert (
        fetch_one(
            "select dictionary_version from sessions where id=$1",
            session["session_id"],
        )
        == 2
    )
    hints = fetch_one(
        "select count(*) from dictionary_terms where user_id=$1 and deleted_at is null",
        u1["user"]["id"],
    )
    assert hints == 0


def test_tc_dict_04_search_is_literal_and_paginated(app_client):
    u1 = new_user(app_client, "dict04-u1@example.com", "dict04-d1")
    u2 = new_user(app_client, "dict04-u2@example.com", "dict04-d2")
    headers = auth_headers(u1)
    for term in ("React", "reactive", "TypeScript", "100% Literal", "under_score"):
        assert create_term(app_client, headers, term).status_code == 201
    assert create_term(app_client, auth_headers(u2), "React").status_code == 201

    needle = list_terms(app_client, headers, q="REACT").json()
    assert {item["term"] for item in needle["items"]} == {"React", "reactive"}
    percent = list_terms(app_client, headers, q="%").json()
    assert [item["term"] for item in percent["items"]] == ["100% Literal"]
    underscore = list_terms(app_client, headers, q="_").json()
    assert [item["term"] for item in underscore["items"]] == ["under_score"]

    page1 = list_terms(app_client, headers, limit=2).json()
    assert len(page1["items"]) == 2
    page2 = list_terms(
        app_client, headers, limit=2, cursor=page1["next_cursor"]
    ).json()
    page3 = list_terms(
        app_client, headers, limit=2, cursor=page2["next_cursor"]
    ).json()
    names = [item["term"] for item in page1["items"] + page2["items"] + page3["items"]]
    assert len(names) == len(set(names)) == 5


def test_tc_dict_08_stale_delete_does_not_override_v2(app_client):
    u1 = new_user(app_client, "dict08@example.com", "dict08-d1")
    headers = auth_headers(u1)
    term = create_term(app_client, headers, "TypeScript", ["ts"]).json()
    updated = app_client.patch(
        f"/api/v1/dictionary/terms/{term['id']}",
        json={"version": 1, "term": "TypeScript", "aliases": ["ts", "tsc"]},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    stale = app_client.delete(
        f"/api/v1/dictionary/terms/{term['id']}",
        params={"version": 1},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    current = list_terms(app_client, headers).json()["items"][0]
    assert current["version"] == 2
    assert current["aliases"] == ["ts", "tsc"]


def test_tc_dict_09_cross_tenant_term_is_404(app_client):
    u1 = new_user(app_client, "dict09-u1@example.com", "dict09-d1")
    u2 = new_user(app_client, "dict09-u2@example.com", "dict09-d2")
    h1 = auth_headers(u1)
    h2 = auth_headers(u2)
    t1 = create_term(app_client, h1, "Kubernetes").json()
    t2 = create_term(app_client, h2, "Kubernetes").json()
    assert t1["id"] != t2["id"]
    assert [item["id"] for item in list_terms(app_client, h1).json()["items"]] == [t1["id"]]
    assert list_terms(app_client, h1).json()["dictionary_version"] == 1
    assert list_terms(app_client, h2).json()["dictionary_version"] == 1
    foreign = app_client.patch(
        f"/api/v1/dictionary/terms/{t2['id']}",
        json={"version": 1, "term": "Hijacked"},
        headers={**h1, "Idempotency-Key": key()},
    )
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "NOT_FOUND"


def test_tc_dict_10_persists_across_restart_and_device(app_client):
    from app.main import create_app

    u1 = new_user(app_client, "dict10@example.com", "dict10-d1")
    headers = auth_headers(u1)
    assert create_term(app_client, headers, "Kubernetes", ["k8s"]).status_code == 201
    before = list_terms(app_client, headers).json()

    second = login(app_client, "dict10@example.com", "dict10-d2")
    assert second.status_code == 200
    from_second = list_terms(app_client, auth_headers(second.json())).json()
    assert from_second == before

    fresh_app = create_app(str(SERVER_ROOT / "config" / "app.yaml"))
    with TestClient(fresh_app) as fresh:
        fresh_tokens = login(fresh, "dict10@example.com", "dict10-d3")
        assert fresh_tokens.status_code == 200, fresh_tokens.text
        after_restart = list_terms(fresh, auth_headers(fresh_tokens.json())).json()
    assert after_restart["dictionary_version"] == before["dictionary_version"]
    assert after_restart["items"] == before["items"]


def test_tc_dict_11_shape_validation_has_no_partial_write(app_client):
    u1 = new_user(app_client, "dict11@example.com", "dict11-d1")
    headers = auth_headers(u1)
    before = list_terms(app_client, headers).json()["dictionary_version"]
    assert create_term(app_client, headers, "React").status_code == 201
    duplicate = create_term(app_client, headers, "Ｒｅａｃｔ")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "TERM_EXISTS"
    invalid = (
        ("", []),
        ("   ", []),
        ("x" * 65, []),
        ("Visual Studio Code", [f"alias-{i}" for i in range(11)]),
        ("Neovim", ["neovim"]),
        ("bad\nterm", []),
        ("bad\x00term", []),
    )
    for term, aliases in invalid:
        response = create_term(app_client, headers, term, aliases)
        assert response.status_code == 422, (term, response.text)
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    spaced = create_term(app_client, headers, "Visual Studio Code")
    assert spaced.status_code == 201, spaced.text
    listed = list_terms(app_client, headers).json()
    assert {item["term"] for item in listed["items"]} == {"React", "Visual Studio Code"}
    assert listed["dictionary_version"] == before + 2
    editable = next(item for item in listed["items"] if item["term"] == "Visual Studio Code")
    for term, aliases, status, code in [
        ("Ｒｅａｃｔ", [], 409, "TERM_EXISTS"),
        *((term, aliases, 422, "VALIDATION_ERROR") for term, aliases in invalid),
    ]:
        response = app_client.patch(
            f"/api/v1/dictionary/terms/{editable['id']}",
            headers={**headers, "Idempotency-Key": key()},
            json={"version": editable["version"], "term": term, "aliases": aliases},
        )
        assert response.status_code == status, (term, response.text)
        assert response.json()["error"]["code"] == code
        # Verify persisted terms, aliases, context, row versions, and the global
        # dictionary version after each failed edit, not merely its response.
        assert list_terms(app_client, headers).json() == listed


def test_tc_dict_12_reimport_skips_identical_and_conflicts_alias_changes(app_client):
    u1 = new_user(app_client, "dict12@example.com", "dict12-d1")
    headers = auth_headers(u1)
    first_csv = b"term,aliases\nKubernetes,k8s\n"
    first = app_client.post(
        "/api/v1/dictionary/import",
        files={"file": ("terms.csv", first_csv, "text/csv")},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert first.status_code == 200, first.text
    version = first.json()["dictionary_version"]
    second = app_client.post(
        "/api/v1/dictionary/import",
        files={"file": ("terms.csv", first_csv, "text/csv")},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert second.status_code == 200
    assert second.json()["created"] == 0
    assert second.json()["skipped"] == 1
    assert second.json()["dictionary_version"] == version
    conflict_csv = b"term,aliases\nKubernetes,kube\n"
    conflict = app_client.post(
        "/api/v1/dictionary/import",
        files={"file": ("terms.csv", conflict_csv, "text/csv")},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert conflict.status_code == 200
    assert conflict.json()["conflicts"] == 1
    assert conflict.json()["dictionary_version"] == version
    term = list_terms(app_client, headers).json()["items"][0]
    assert term["aliases"] == ["k8s"]


def test_tc_dict_13_csv_bom_quotes_limits_and_zero_write(app_client):
    u1 = new_user(app_client, "dict13@example.com", "dict13-d1")
    headers = auth_headers(u1)
    body = (
        b"\xef\xbb\xbf"
        + '"term",aliases\n"Hello, World","hw;hello world"\nDocker,dkr\n'.encode()
    )
    response = app_client.post(
        "/api/v1/dictionary/import",
        files={"file": ("terms.csv", body, "text/csv")},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 2, response.json()
    terms = {item["term"]: item for item in list_terms(app_client, headers).json()["items"]}
    assert terms["Hello, World"]["aliases"] == ["hw", "hello world"]
    version = list_terms(app_client, headers).json()["dictionary_version"]

    too_big = b"term,aliases\n" + b"a" * (1024 * 1024)
    big = app_client.post(
        "/api/v1/dictionary/import",
        files={"file": ("big.csv", too_big, "text/csv")},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert big.status_code == 413
    rows = b"term,aliases\n" + b"".join(
        f"Term{i},\n".encode() for i in range(1001)
    )
    too_many = app_client.post(
        "/api/v1/dictionary/import",
        files={"file": ("rows.csv", rows, "text/csv")},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert too_many.status_code == 413
    for bad in (b"name,aliases\nfoo,bar\n", b"term,term\nfoo,bar\n", b"\xff\xfe\x00"):
        rejected = app_client.post(
            "/api/v1/dictionary/import",
            files={"file": ("bad.csv", bad, "text/csv")},
            headers={**headers, "Idempotency-Key": key()},
        )
        assert rejected.status_code == 422
    after = list_terms(app_client, headers).json()
    assert after["dictionary_version"] == version
    assert len(after["items"]) == 2


def test_tc_dict_15_session_freezes_dictionary_version(app_client):
    u1 = new_user(app_client, "dict15@example.com", "dict15-d1")
    headers = auth_headers(u1)
    assert create_term(app_client, headers, "Kubernetes").status_code == 201
    session = create_session(app_client, headers, "dict15-s1").json()
    assert (
        fetch_one(
            "select dictionary_version from sessions where id=$1",
            session["session_id"],
        )
        == 1
    )
    assert create_term(app_client, headers, "Docker").status_code == 201
    assert list_terms(app_client, headers).json()["dictionary_version"] == 2
    events = dictation(app_client, headers, session["session_id"])
    result = [e for e in events if e["type"] == "result.ready"][0]["result"]
    assert result["dictionary_version"] == 1
    session2 = create_session(app_client, headers, "dict15-s2").json()
    assert (
        fetch_one(
            "select dictionary_version from sessions where id=$1",
            session2["session_id"],
        )
        == 2
    )


def test_tc_dict_16_shared_alias_is_ambiguous(app_client):
    u1 = new_user(app_client, "dict16@example.com", "dict16-d1")
    headers = auth_headers(u1)
    assert create_term(app_client, headers, "Claude", ["cloud"]).status_code == 201
    assert create_term(app_client, headers, "Claw", ["cloud"]).status_code == 201
    items = list_terms(app_client, headers).json()["items"]
    by_term = {item["term"]: item for item in items}
    assert "cloud" in by_term["Claude"]["ambiguous_aliases"]
    assert "cloud" in by_term["Claw"]["ambiguous_aliases"]


def _wait_event(client, headers, event_id, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/learning-events/{event_id}", headers=headers)
        assert response.status_code == 200, response.text
        if response.json()["status"] != "pending":
            return response.json()
        time.sleep(0.05)
    raise AssertionError("learning event stayed pending")


def _suggested_event(app_client, headers, request_id):
    session = create_session(app_client, headers, request_id).json()
    events = dictation(app_client, headers, session["session_id"])
    result = events[-1]["result"]
    insertion_id = key()
    claimed = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/claim",
        json={
            "result_id": result["result_id"],
            "insertion_id": insertion_id,
            "text_hash": result["text_hash"],
            "review_confirmed": False,
        },
        headers=headers,
    )
    assert claimed.status_code == 201, claimed.text
    receipt = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}/receipt",
        json={"status": "inserted", "observable": True, "error_code": None},
        headers=headers,
    )
    assert receipt.status_code == 200, receipt.text
    text = result["refined_text"]
    offset = text.index("按钮")
    correction = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": session["session_id"],
            "insertion_id": insertion_id,
            "correction_id": key(),
            "offset": offset,
            "before": "按钮",
            "after": "按键",
        },
        headers=headers,
    )
    assert correction.status_code == 202, correction.text
    return _wait_event(app_client, headers, correction.json()["event_id"])


def test_tc_dict_06_undo_is_idempotent(app_client):
    u1 = new_user(app_client, "dict06@example.com", "dict06-d1")
    headers = auth_headers(u1)
    event = _suggested_event(app_client, headers, "dict06-session")
    confirmed = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/confirm",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert confirmed.status_code == 200, confirmed.text
    learned = confirmed.json()
    assert learned["status"] == "learned"
    version = list_terms(app_client, headers).json()["dictionary_version"]
    undo_key = key()
    first = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/undo",
        headers={**headers, "Idempotency-Key": undo_key},
    )
    assert first.status_code == 200, first.text
    assert first.json()["undone"] is True
    assert first.json()["term"] is None
    assert first.json()["dictionary_version"] == version + 1
    replay = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/undo",
        headers={**headers, "Idempotency-Key": undo_key},
    )
    assert replay.status_code == 200
    assert replay.json()["dictionary_version"] == version + 1
    again = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/undo",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert again.status_code == 200
    assert again.json()["dictionary_version"] == version + 1
    assert list_terms(app_client, headers).json()["items"] == []


def test_tc_dict_14_undo_conflicts_after_manual_edit_or_delete(app_client):
    u1 = new_user(app_client, "dict14@example.com", "dict14-d1")
    headers = auth_headers(u1)
    event = _suggested_event(app_client, headers, "dict14-session")
    confirmed = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/confirm",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert confirmed.status_code == 200, confirmed.text
    term = confirmed.json()["term"]
    assert term["version"] == 1
    for expected in (2, 3):
        edited = app_client.patch(
            f"/api/v1/dictionary/terms/{term['id']}",
            json={"version": expected - 1, "aliases": [f"alias-{expected}"]},
            headers={**headers, "Idempotency-Key": key()},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["version"] == expected
    blocked = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/undo",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "VERSION_CONFLICT"
    current = list_terms(app_client, headers).json()["items"][0]
    assert current["version"] == 3

    deleted = app_client.delete(
        f"/api/v1/dictionary/terms/{term['id']}",
        params={"version": 3},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert deleted.status_code == 200
    after_delete = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/undo",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert after_delete.status_code == 409
    assert list_terms(app_client, headers).json()["items"] == []


def test_tc_dict_17_limit_and_prompt_budgets(app_client):
    u1 = new_user(app_client, "dict17-u1@example.com", "dict17-d1")
    u2 = new_user(app_client, "dict17-u2@example.com", "dict17-d2")
    h1 = auth_headers(u1)
    h2 = auth_headers(u2)
    assert create_term(app_client, h2, "U2Secret").status_code == 201
    user1 = u1["user"]["id"]
    execute_sql(
        """
        insert into dictionary_terms
          (id, user_id, term, norm, aliases, contexts, scope, source, version,
           created_at, updated_at)
        select 'd' || lpad(g::text, 7, '0') || '-0000-4000-8000-' ||
               lpad(g::text, 10, '0'),
               $1, 'Term' || g, lower('Term' || g), '[]'::jsonb, '[]'::jsonb,
               'default', 'manual', 1, now(), now()
        from generate_series(1, 5000) g
        """,
        user1,
    )
    execute_sql(
        "insert into dictionary_state (user_id, version, updated_at) "
        "values ($1, 5000, now()) on conflict (user_id) "
        "do update set version=5000, updated_at=now()",
        user1,
    )
    overflow = create_term(app_client, h1, "OneMore")
    assert overflow.status_code == 409
    assert overflow.json()["error"]["code"] == "DICTIONARY_LIMIT"

    session = create_session(app_client, h1, "dict17-session").json()
    runtime = app_client.app.state.sessions.runtimes[session["session_id"]]
    assert 0 < len(runtime.hint_terms) <= 100
    assert sum(len(term) for term in runtime.hint_terms) <= 2000
    from app.services.dictionary_prompt import refine_hints
    import json
    # The full snapshot is memory-only until ASR tells us which terms match.
    assert len(runtime.dictionary_snapshot) == 5000
    assert runtime.refine_terms == []
    assert fetch_one("select llm_hints from sessions where id=$1", session["session_id"]) is None
    raw = " ".join("Term" + str(i) for i in range(1, 101))
    selected = refine_hints(runtime.dictionary_snapshot, raw)
    assert 0 < len(selected) <= 50
    assert len(json.dumps(selected, ensure_ascii=False, separators=(",", ":"))) <= 4000
    assert "U2Secret" not in runtime.hint_terms
    assert all(item["term"].lower() != "u2secret" for item in runtime.dictionary_snapshot)
    first_hints, first_terms = list(runtime.hint_terms), selected
    app_client.post(f"/api/v1/sessions/{session['session_id']}/cancel", headers=h1)
    assert runtime.dictionary_snapshot == []
    session2 = create_session(app_client, h1, "dict17-session-2").json()
    runtime2 = app_client.app.state.sessions.runtimes[session2["session_id"]]
    assert runtime2.hint_terms == first_hints
    assert refine_hints(runtime2.dictionary_snapshot, raw) == first_terms


def test_tc_dict_18_transaction_failure_rolls_back_everything(app_client, monkeypatch):
    from app.services import dictionary_service

    u1 = new_user(app_client, "dict18@example.com", "dict18-d1")
    headers = auth_headers(u1)
    event = _suggested_event(app_client, headers, "dict18-session")
    before_terms = list_terms(app_client, headers).json()
    original = dictionary_service.bump_version

    async def boom(*args, **kwargs):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(dictionary_service, "bump_version", boom)
    try:
        app_client.post(
            f"/api/v1/learning-events/{event['event_id']}/confirm",
            headers={**headers, "Idempotency-Key": fixture_uuid("dict18-confirm")},
        )
    except RuntimeError:
        pass
    monkeypatch.setattr(dictionary_service, "bump_version", original)

    after_failure = list_terms(app_client, headers).json()
    assert after_failure["dictionary_version"] == before_terms["dictionary_version"]
    assert after_failure["items"] == before_terms["items"]
    fetched = app_client.get(
        f"/api/v1/learning-events/{event['event_id']}", headers=headers
    ).json()
    assert fetched["status"] == "suggested"
    assert fetched["term"] is None

    retried = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/confirm",
        headers={**headers, "Idempotency-Key": fixture_uuid("dict18-confirm")},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "learned"
    final = list_terms(app_client, headers).json()
    assert len(final["items"]) == 1
    assert final["dictionary_version"] == before_terms["dictionary_version"] + 1
