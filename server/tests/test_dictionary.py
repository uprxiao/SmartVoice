from __future__ import annotations

import uuid

from tests.conftest import auth_headers, register_and_login


def key() -> str:
    return str(uuid.uuid4())


def create_term(client, headers, term, aliases=None, idem=None):
    response = client.post(
        "/api/v1/dictionary/terms",
        json={"term": term, "aliases": aliases or [], "scope": "default"},
        headers={**headers, "Idempotency-Key": idem or key()},
    )
    return response


def test_create_list_and_search_terms(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)

    created = create_term(app_client, headers, "Kubernetes", ["k8s"])
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["term"] == "Kubernetes"
    assert body["source"] == "manual"
    assert body["version"] == 1
    assert body["dictionary_version"] == 1
    assert body["replayed"] is False

    listed = app_client.get("/api/v1/dictionary/terms", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["dictionary_version"] == 1
    assert [item["term"] for item in listed.json()["items"]] == ["Kubernetes"]

    by_alias = app_client.get(
        "/api/v1/dictionary/terms", params={"q": "k8s"}, headers=headers
    )
    assert [item["term"] for item in by_alias.json()["items"]] == ["Kubernetes"]

    by_norm = app_client.get(
        "/api/v1/dictionary/terms", params={"q": "kubernetes"}, headers=headers
    )
    assert len(by_norm.json()["items"]) == 1


def test_duplicate_norm_is_rejected_casefold_and_nfkc(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    assert create_term(app_client, headers, "Kubernetes").status_code == 201
    duplicate = create_term(app_client, headers, "ｋｕｂｅｒｎｅｔｅｓ")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "TERM_EXISTS"


def test_create_is_idempotent(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    idem = key()
    first = create_term(app_client, headers, "Docker", [], idem)
    second = create_term(app_client, headers, "Docker", [], idem)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["id"] == first.json()["id"]


def test_patch_version_cas_and_replacement(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    term = create_term(app_client, headers, "TypeScript", ["ts"]).json()

    stale = app_client.patch(
        f"/api/v1/dictionary/terms/{term['id']}",
        json={"version": 99, "aliases": ["typescript"]},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    unchanged = app_client.patch(
        f"/api/v1/dictionary/terms/{term['id']}",
        json={"version": 1, "term": "TypeScript", "aliases": ["ts"]},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["version"] == 1
    assert unchanged.json()["dictionary_version"] == 1

    updated = app_client.patch(
        f"/api/v1/dictionary/terms/{term['id']}",
        json={"version": 1, "term": "TS", "aliases": ["typescript"]},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert updated.status_code == 200
    assert updated.json()["term"] == "TS"
    assert updated.json()["aliases"] == ["typescript"]
    assert updated.json()["version"] == 2
    assert updated.json()["dictionary_version"] == 2


def test_patch_rejects_alias_equal_to_term(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    term = create_term(app_client, headers, "React").json()
    response = app_client.patch(
        f"/api/v1/dictionary/terms/{term['id']}",
        json={"version": 1, "aliases": ["react"]},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert response.status_code == 422


def test_delete_is_soft_and_idempotent(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    term = create_term(app_client, headers, "Vue").json()
    idem = key()
    first = app_client.delete(
        f"/api/v1/dictionary/terms/{term['id']}",
        params={"version": 1},
        headers={**headers, "Idempotency-Key": idem},
    )
    assert first.status_code == 200
    assert first.json()["deleted"] is True
    assert first.json()["version"] == 2

    replay = app_client.delete(
        f"/api/v1/dictionary/terms/{term['id']}",
        params={"version": 1},
        headers={**headers, "Idempotency-Key": idem},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True

    listed = app_client.get("/api/v1/dictionary/terms", headers=headers)
    assert listed.json()["items"] == []

    recreated = create_term(app_client, headers, "Vue")
    assert recreated.status_code == 201
    assert recreated.json()["id"] != term["id"]


def test_cursor_is_bound_to_dictionary_version(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    for name in ("Alpha", "Beta", "Gamma"):
        assert create_term(app_client, headers, name).status_code == 201
    page = app_client.get(
        "/api/v1/dictionary/terms", params={"limit": 2}, headers=headers
    ).json()
    assert len(page["items"]) == 2
    assert page["next_cursor"]

    create_term(app_client, headers, "Delta")
    stale = app_client.get(
        "/api/v1/dictionary/terms",
        params={"limit": 2, "cursor": page["next_cursor"]},
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "DICTIONARY_CHANGED"


def test_csv_import_reports_each_row(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    csv_body = (
        "term,aliases\n"
        "Kubernetes,k8s\n"
        "Kubernetes,k8s\n"
        "Kubernetes,kube\n"
        '"line\nbreak",\n'
        "Docker,\n"
    ).encode()
    idem = key()
    response = app_client.post(
        "/api/v1/dictionary/import",
        files={"file": ("terms.csv", csv_body, "text/csv")},
        headers={**headers, "Idempotency-Key": idem},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 2
    assert body["skipped"] == 1
    assert body["conflicts"] == 1
    assert body["invalid"] == 1
    statuses = {(row["row"], row["status"]) for row in body["rows"]}
    assert (2, "created") in statuses
    assert (3, "skipped") in statuses
    assert (4, "conflict") in statuses
    assert (5, "invalid") in statuses
    assert (6, "created") in statuses
    assert body["replayed"] is False

    replay = app_client.post(
        "/api/v1/dictionary/import",
        files={"file": ("terms.csv", csv_body, "text/csv")},
        headers={**headers, "Idempotency-Key": idem},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["created"] == 2


def test_csv_structure_errors_reject_whole_file(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    for content in (
        b"name,aliases\nfoo,bar\n",
        b"term,aliases,extra\nfoo,bar,baz\n",
        b"term,term\nfoo,bar\n",
        b"\xff\xfe\x00",
    ):
        response = app_client.post(
            "/api/v1/dictionary/import",
            files={"file": ("bad.csv", content, "text/csv")},
            headers={**headers, "Idempotency-Key": key()},
        )
        assert response.status_code == 422, (content, response.text)
    listed = app_client.get("/api/v1/dictionary/terms", headers=headers)
    assert listed.json()["items"] == []
