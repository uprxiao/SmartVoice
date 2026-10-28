from __future__ import annotations

from tests.ids import fixture_uuid

from app.services.rate_limit import LoginRateLimiter
from tests.conftest import register_and_login


def login_attempt(app_client, email: str = "user@example.com"):
    return app_client.post(
        "/api/auth/login",
        json={
            "email": email,
            "password": "SmokePass123",
            "client_type": "desktop",
            "device": {
                "installation_id": fixture_uuid("test-installation"),
                "name": "test-mac",
                "platform": "macos",
                "client_version": "0.1.0",
            },
        },
    )


def test_login_ip_window_returns_429_with_retry_after(app_client, monkeypatch):
    # Keep this quota assertion inside one UTC window, regardless of wall time.
    monkeypatch.setattr("app.services.rate_limit.utc_seconds", lambda: 1_800_000_001.0)
    register_and_login(app_client)
    # The registration helper already consumed one attempt.
    for _ in range(9):
        assert login_attempt(app_client).status_code == 200
    blocked = login_attempt(app_client)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.json()["error"]["retryable"] is True

    # A different account from the same IP shares the IP budget.
    registered = app_client.post(
        "/api/auth/register",
        json={"email": "second@example.com", "password": "SmokePass123"},
    )
    assert registered.status_code == 201
    other = login_attempt(app_client, email="second@example.com")
    assert other.status_code == 429


def test_rate_limiter_isolates_ips_and_accounts(monkeypatch):
    monkeypatch.setattr("app.services.rate_limit.utc_seconds", lambda: 1_800_000_001.0)
    limiter = LoginRateLimiter()
    for _ in range(10):
        assert limiter.check("10.0.0.1", "a@example.com") is None
    assert limiter.check("10.0.0.1", "a@example.com") is not None
    # Another IP has an independent budget even for the same account.
    assert limiter.check("10.0.0.2", "a@example.com") is None
    # Another account from the throttled IP is still limited by IP.
    assert limiter.check("10.0.0.1", "b@example.com") is not None


def test_email_budget_across_many_ips(monkeypatch):
    monkeypatch.setattr("app.services.rate_limit.utc_seconds", lambda: 1_800_000_001.0)
    limiter = LoginRateLimiter()
    for index in range(20):
        assert limiter.check(f"10.0.1.{index}", "target@example.com") is None
    assert limiter.check("10.0.2.1", "target@example.com") is not None
    assert limiter.check("10.0.2.1", "someone-else@example.com") is None
