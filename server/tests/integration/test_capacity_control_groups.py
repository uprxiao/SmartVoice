"""Real PostgreSQL control-plane groups; stub providers never prove throughput.

Reuse conftest's process-owned DB and app_client lifespan. No database lifecycle,
retention/watchdog clock patch, inference, audio generation, or SQL admission seed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import secrets
import uuid

import httpx
import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.engine import make_url

from app.core.config import Config, SESSION_PCM_RESERVE
from app.core.security import create_access_token, hash_password, new_refresh_token, utcnow, verify_password
from app.models import CapacityChange, Device, DictionaryState, LoginSession, ModelCall, SessionAttempt, User, VoiceSession
from app.models.session import ACTIVE_STATES
from tests.conftest import TEST_DB


@dataclass(frozen=True)
class Actor:
    user_id: str
    sid: str
    request_id: str
    access_token: str = field(repr=False)

    def headers(self):
        return {"Authorization": "Bearer " + self.access_token}


@pytest.fixture(autouse=True)
def _mock_expanded_deployment(monkeypatch):
    # Autouse runs before app_client. Validate the complete config before
    # startup so provider queue limits derive from the same expanded ceiling.
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    from app import main as main_module

    load = main_module.load_config

    def expanded(path=None):
        base = load(path)
        payload = base.model_dump()
        payload["sessions"]["admission_ceiling"] = 100
        payload["audio"]["active_pcm_budget_bytes"] = 100 * SESSION_PCM_RESERVE
        validated = Config.model_validate(payload)
        # model_copy keeps environment-derived private DB/secret attributes.
        return base.model_copy(update={"sessions": validated.sessions, "audio": validated.audio})

    monkeypatch.setattr(main_module, "load_config", expanded)


async def _seed_actors(app, users):
    config = app.state.config
    assert make_url(config.database_url).database == TEST_DB
    assert TEST_DB.startswith("smartvoiceinput_test_")
    assert config.asr.provider == config.llm.provider == "mock"
    assert config.effective_admission_ceiling == 100
    password = secrets.token_urlsafe(24)
    encoded = await asyncio.to_thread(hash_password, config.auth, password)
    valid = await asyncio.to_thread(verify_password, config.auth, password, encoded)
    if not valid or not encoded.startswith("$argon2id$v=19$m=65536,t=3,p=1$"):
        raise AssertionError("fixture requires a verified contract-compliant Argon2id hash")
    del password
    user_rows, states, devices, logins, actors = [], [], [], [], []
    now = utcnow()
    for index in range(users + 2):
        is_admin = index >= users
        user_id, sid = str(uuid.uuid4()), str(uuid.uuid4())
        device_id = None if is_admin else str(uuid.uuid4())
        role = "admin" if is_admin else "user"
        user_rows.append({"id": user_id, "email": f"cap-control-{index}@example.invalid",
                          "password_hash": encoded, "role": role, "status": "active"})
        states.append({"user_id": user_id, "version": 0})
        if device_id:
            devices.append({"id": device_id, "user_id": user_id, "installation_id": str(uuid.uuid4()),
                            "name": "Capacity fixture", "platform": "macos", "client_version": "0.0.1"})
        _, refresh_digest = new_refresh_token()
        logins.append({"id": sid, "user_id": user_id, "device_id": device_id,
                       "client_type": "admin_web" if is_admin else "desktop", "refresh_hash": refresh_digest,
                       "refresh_expires_at": now + timedelta(days=30)})
        access, _ = create_access_token(config.auth, config.jwt_secret, user_id, sid, role)
        actors.append(Actor(user_id, sid, str(uuid.uuid4()), access))
    async with app.state.db.session_factory() as db:
        async with db.begin():
            await db.execute(insert(User), user_rows)
            await db.execute(insert(DictionaryState), states)
            if devices:
                await db.execute(insert(Device), devices)
            await db.execute(insert(LoginSession), logins)
    return actors[:users], actors[users:]


def _error(response):
    body = response.json()
    return {"status": response.status_code, "code": body.get("error", {}).get("code")}


async def _view(http, actor):
    response = await http.get("/api/admin/capacity", headers=actor.headers())
    assert response.status_code == 200, _error(response)
    return response.json()


async def _patch(http, actor, version, value):
    return await http.patch("/api/admin/capacity", headers=actor.headers(),
                            json={"version": version, "max_active_sessions": value})


async def _create(http, actor, key=None):
    key = key or actor.request_id
    return await http.post("/api/v1/sessions", headers=actor.headers() | {"Idempotency-Key": key},
                           json={"client_request_id": key, "mode": "dictation", "learning_enabled": False})


async def _snapshot(app):
    async with app.state.db.session_factory() as db:
        rows = list((await db.execute(select(VoiceSession.id, VoiceSession.user_id,
                    VoiceSession.client_request_id, VoiceSession.state, VoiceSession.current_attempt_no,
                    VoiceSession.error_code))).mappings())
        attempts = int(await db.scalar(select(func.count()).select_from(SessionAttempt)))
        calls = int(await db.scalar(select(func.count()).select_from(ModelCall)))
        changes = list((await db.execute(select(CapacityChange.actor_user_id, CapacityChange.old_max_active,
                    CapacityChange.new_max_active, CapacityChange.from_version, CapacityChange.to_version)
                    .order_by(CapacityChange.to_version))).mappings())
    return {"rows": [dict(row) for row in rows], "attempts": attempts, "model_calls": calls,
            "changes": [dict(row) for row in changes]}


def _active(snapshot):
    return [row for row in snapshot["rows"] if row["state"] in ACTIVE_STATES]


async def _simultaneous(calls):
    barrier = asyncio.Barrier(len(calls))

    async def invoke(call):
        await barrier.wait()
        return await call()

    async with asyncio.timeout(15):
        return await asyncio.gather(*(invoke(call) for call in calls))


async def _blocked_create_burst(app, http, actors, n):
    """Every authenticated request reaches the barrier before real admission.

    Accepted requests block only at Mock ASR.open outside DB transactions; rejected
    requests can finish. Existing 10-second connect deadlines are kept unchanged.
    """
    manager = app.state.sessions
    original_create = manager.create
    provider = manager.asr_provider
    original_open = provider.open
    barrier = asyncio.Barrier(2 * n)
    release, settled = asyncio.Event(), asyncio.Event()
    opened, returned = [], []
    arrivals = 0

    def progress():
        if len(opened) + len(returned) == 2 * n:
            settled.set()

    async def create_at_barrier(ctx, client_request_id, learning_enabled):
        nonlocal arrivals
        arrivals += 1
        await barrier.wait()
        return await original_create(ctx, client_request_id, learning_enabled)

    async def gated_open(options):
        stream = await original_open(options)
        opened.append(options.session_id)
        progress()
        try:
            await release.wait()
            return stream
        except BaseException:
            await stream.cancel()
            raise

    async def request(actor):
        response = await _create(http, actor)
        returned.append(response)
        progress()
        return response

    manager.create, provider.open = create_at_barrier, gated_open
    tasks = [asyncio.create_task(request(actor)) for actor in actors]
    try:
        async with asyncio.timeout(8):
            await settled.wait()
            assert arrivals == 2 * n
            assert len(opened) == n and len(set(opened)) == n
            assert len(returned) == n
            assert all(r.status_code == 429 and r.json()["error"]["code"] == "CAPACITY_EXCEEDED"
                       for r in returned), [_error(r) for r in returned]
            frozen = await _snapshot(app)
            assert len(_active(frozen)) == n
            assert len(frozen["rows"]) == frozen["attempts"] == n
            assert set(opened) == {row["id"] for row in _active(frozen)}
            assert frozen["model_calls"] == 0
        release.set()
        async with asyncio.timeout(8):
            responses = await asyncio.gather(*tasks)
        assert [r.status_code for r in responses].count(201) == n
        assert [r.status_code for r in responses].count(429) == n
        assert all(r.status_code in (201, 429) for r in responses)
        return list(zip(actors, responses))
    finally:
        release.set()
        manager.create, provider.open = original_create, original_open
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _cancel(http, pairs):
    async def one(actor, session_id):
        response = await http.post(f"/api/v1/sessions/{session_id}/cancel", headers=actor.headers())
        assert response.status_code == 200, _error(response)
        assert response.json()["state"] == "cancelled"
    await asyncio.gather(*(one(actor, sid) for actor, sid in pairs))


async def _assert_parallel_cas(app, http, admins, choices):
    reads = await asyncio.gather(*(_view(http, actor) for actor in admins))
    before = reads[0]["config"]
    assert reads[1]["config"] == before
    audit_before = (await _snapshot(app))["changes"]
    responses = await _simultaneous([
        lambda: _patch(http, admins[0], before["version"], choices[0]),
        lambda: _patch(http, admins[1], before["version"], choices[1]),
    ])
    assert sorted(r.status_code for r in responses) == [200, 409], [_error(r) for r in responses]
    winning_index = next(i for i, response in enumerate(responses) if response.status_code == 200)
    stale = next(r for r in responses if r.status_code == 409)
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    assert stale.json()["error"]["details"]["current_version"] == before["version"] + 1
    current = (await _view(http, admins[0]))["config"]
    assert current["version"] == before["version"] + 1
    assert current["max_active_sessions"] == choices[winning_index]
    audit_after = (await _snapshot(app))["changes"]
    assert audit_after[:-1] == audit_before
    assert audit_after[-1] == {"actor_user_id": admins[winning_index].user_id,
        "old_max_active": before["max_active_sessions"], "new_max_active": choices[winning_index],
        "from_version": before["version"], "to_version": before["version"] + 1}
    assert len(audit_after) == len(audit_before) + 1
    noop = await _simultaneous([
        lambda: _patch(http, admins[0], current["version"], current["max_active_sessions"]),
        lambda: _patch(http, admins[1], current["version"], current["max_active_sessions"]),
    ])
    assert all(r.status_code == 200 and r.json()["config"] == current for r in noop)
    assert (await _snapshot(app))["changes"] == audit_after
    assert (await _view(http, admins[1]))["config"] == current


def test_tc_cap_04_two_admins_parallel_cas_and_same_value_noop(app_client):
    async def run():
        _, admins = await _seed_actors(app_client.app, 0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_client.app), base_url="http://testserver") as http:
            initial = (await _view(http, admins[0]))["config"]
            assert initial["version"] == 1 and initial["max_active_sessions"] == 20
            await _assert_parallel_cas(app_client.app, http, admins, (10, 15))
    app_client.portal.call(run)


@pytest.mark.parametrize("n,transaction_delay_seconds", [
    pytest.param(20, 0, id="N20"),
    pytest.param(50, 0, id="N50"),
    pytest.param(100, 0, id="N100"),
    pytest.param(100, .005, id="N100-lock-5ms"),
])
def test_tc_cap_03_23_expanded_admission_idempotency_drain_and_cas(
    app_client, monkeypatch, n, transaction_delay_seconds,
):
    async def run():
        app = app_client.app
        if transaction_delay_seconds:
            original_lock = app.state.capacity.lock

            async def bounded_transaction_latency(db):
                value = await original_lock(db)
                # Reproduce ordinary short work while holding the capacity row.
                # Pool size, auth/connection timeouts and every assertion stay
                # unchanged; waiting admissions must not starve live probes.
                await asyncio.sleep(transaction_delay_seconds)
                return value

            monkeypatch.setattr(app.state.capacity, "lock", bounded_transaction_latency)
        actors, admins = await _seed_actors(app, 2 * n)
        actor_by_user = {actor.user_id: actor for actor in actors}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            try:
                initial = await _view(http, admins[0])
                assert initial["deployment"]["effective_ceiling"] == 100
                assert initial["deployment"]["active_pcm_budget_bytes"] >= n * SESSION_PCM_RESERVE
                if n == 20:
                    # Exercise an actual saved change back to the baseline 20,
                    # rather than claiming that the initial same-value no-op
                    # increments the version or creates an audit entry.
                    lowered = await _patch(http, admins[0], initial["config"]["version"], 10)
                    assert lowered.status_code == 200, _error(lowered)
                    initial = await _view(http, admins[0])
                    assert initial["config"]["max_active_sessions"] == 10
                saved = await _patch(http, admins[0], initial["config"]["version"], n)
                assert saved.status_code == 200, _error(saved)
                config = (await _view(http, admins[1]))["config"]
                assert config["max_active_sessions"] == n
                assert config["version"] == initial["config"]["version"] + 1
                assert (await _snapshot(app))["changes"][-1]["new_max_active"] == n
                outcomes = await _blocked_create_burst(app, http, actors, n)
                accepted = [(actor, response.json()["session_id"]) for actor, response in outcomes if response.status_code == 201]
                rejected = [actor for actor, response in outcomes if response.status_code == 429]
                winner = accepted[0][0]
                checks = await _simultaneous([
                    lambda: _create(http, winner), lambda: _create(http, winner),
                    lambda: _create(http, winner, str(uuid.uuid4())),
                    lambda: _create(http, winner, str(uuid.uuid4())),
                ])
                assert [r.status_code for r in checks] == [200, 200, 409, 409]
                assert all(r.json()["replayed"] is True and r.json()["session_id"] == accepted[0][1] for r in checks[:2])
                assert all(r.json()["error"]["code"] == "ACTIVE_SESSION_EXISTS" for r in checks[2:])
                snapshot = await _snapshot(app)
                assert len(_active(snapshot)) == len(snapshot["rows"]) == snapshot["attempts"] == n
                assert all(row["state"] == "created" and row["current_attempt_no"] == 1 and row["error_code"] is None
                           for row in _active(snapshot))
                lower = n // 2
                changed = await _patch(http, admins[0], config["version"], lower)
                assert changed.status_code == 200, _error(changed)
                assert len(_active(await _snapshot(app))) == n  # lowering never cancels existing work
                capabilities = await http.get("/api/v1/capabilities", headers=winner.headers())
                assert capabilities.status_code == 200
                assert capabilities.json()["limits"]["global_sessions"] == lower
                for remaining, cancellations in ((n, []), (lower, accepted[:n-lower])):
                    await _cancel(http, cancellations)
                    assert len(_active(await _snapshot(app))) == remaining
                    full = await _create(http, rejected[0])
                    assert full.status_code == 429 and full.json()["error"]["code"] == "CAPACITY_EXCEEDED"
                await _cancel(http, [accepted[n-lower]])
                assert len(_active(await _snapshot(app))) == lower - 1
                recovered = await _create(http, rejected[0])  # same key as prior 429s
                assert recovered.status_code == 201 and recovered.json()["replayed"] is False
                assert len(_active(await _snapshot(app))) == lower
                survivors = accepted[n-lower+1:]
                await _cancel(http, survivors + [(rejected[0], recovered.json()["session_id"])])
                assert not _active(await _snapshot(app))
                # Fresh parallel duplicate requests after drain also reserve only one slot.
                cold = rejected[1]
                duplicate = await _simultaneous([lambda: _create(http, cold), lambda: _create(http, cold)])
                assert sorted(r.status_code for r in duplicate) == [200, 201]
                assert len({r.json()["session_id"] for r in duplicate}) == 1
                assert len(_active(await _snapshot(app))) == 1
                await _cancel(http, [(cold, duplicate[0].json()["session_id"])])
                await _assert_parallel_cas(app, http, admins, (lower + 1, lower + 2))
                final = await _snapshot(app)
                assert not _active(final)
                assert final["model_calls"] == 0
                assert len(final["rows"]) == final["attempts"] == n + 2
            finally:
                # Best-effort cleanup uses the real cancellation API, never SQL DELETE/TRUNCATE.
                active = _active(await _snapshot(app))
                await asyncio.gather(*(http.post(f"/api/v1/sessions/{row['id']}/cancel",
                    headers=actor_by_user[row["user_id"]].headers()) for row in active), return_exceptions=True)
    app_client.portal.call(run)
