"""Real isolated PostgreSQL drafts; no inference or real-model acceptance."""
import asyncio
import uuid

from sqlalchemy import event, select

from app.models import LoginSession
from app.services.runtime_auth_batch import RuntimeAuthBatch, RuntimeAuthKey, RuntimeAuthUnavailable
from tests.conftest import auth_headers, register_and_login
from tests.test_sessions import create_session


def test_batch_query_keeps_exact_tenant_and_missing_row_semantics(app_client):
    tokens = [register_and_login(app_client, email=f"auth-batch-{i}@example.invalid") for i in range(2)]
    created = create_session(app_client, auth_headers(tokens[0]), str(uuid.uuid4()))
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    user_a, user_b = [token["user"]["id"] for token in tokens]

    async def run():
        db_owner = app_client.app.state.db
        async with db_owner.session_factory() as db:
            sid_a = await db.scalar(select(LoginSession.id).where(LoginSession.user_id == user_a))
            sid_b = await db.scalar(select(LoginSession.id).where(LoginSession.user_id == user_b))
        identities = [RuntimeAuthKey(user_a, sid_a, session_id),
                      RuntimeAuthKey(user_a, sid_b, session_id),
                      RuntimeAuthKey(user_b, sid_b, session_id),
                      RuntimeAuthKey(str(uuid.uuid4()), sid_a, session_id),
                      RuntimeAuthKey(user_a, None, session_id)]
        reader = RuntimeAuthBatch(db_owner)
        try:
            deadline = asyncio.get_running_loop().time() + 1
            rows = await asyncio.gather(*(reader.read(identity, deadline) for identity in identities))
            assert tuple(value.id for value in rows[0]) == (user_a, sid_a, session_id)
            assert rows[1][0].id == user_a and rows[1][1] is None and rows[1][2].id == session_id
            assert rows[2][0].id == user_b and rows[2][1].id == sid_b and rows[2][2] is None
            assert rows[3] == (None, None, None)
            assert rows[4][0].id == user_a and rows[4][1] is None and rows[4][2].id == session_id
        finally:
            await reader.close()
    async def bounded():
        # Overall test anti-hang, never passed as a runtime authorization budget.
        async with asyncio.timeout(5):
            await run()
    app_client.portal.call(bounded)


def test_real_query_failure_fails_entire_batch_then_next_read_recovers(app_client):
    token = register_and_login(app_client, email="auth-batch-failure@example.invalid")
    user_id = token["user"]["id"]

    async def run():
        db_owner = app_client.app.state.db
        async with db_owner.session_factory() as db:
            sid = await db.scalar(select(LoginSession.id).where(LoginSession.user_id == user_id))
        identity = RuntimeAuthKey(user_id, sid, str(uuid.uuid4()))
        reader = RuntimeAuthBatch(db_owner)
        engine = db_owner.engine.sync_engine
        injected, sqlstates = False, []
        def before(conn, cursor, statement, params, context, many):
            nonlocal injected
            if not injected and "runtime_auth_checks" in statement:
                injected = True
                return "SELECT 1 / 0", ()
            return statement, params
        def failed(context):
            exc = context.original_exception
            sqlstates.append(getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None))
        event.listen(engine, "before_cursor_execute", before, retval=True)
        event.listen(engine, "handle_error", failed)
        try:
            deadline = asyncio.get_running_loop().time() + 1
            outcomes = await asyncio.gather(*(reader.read(identity, deadline) for _ in range(3)), return_exceptions=True)
            assert injected and sqlstates == ["22012"]
            assert all(isinstance(outcome, RuntimeAuthUnavailable) for outcome in outcomes)
            assert all("SELECT" not in str(outcome) for outcome in outcomes)
            user, login, session = await reader.read(identity, asyncio.get_running_loop().time() + 1)
            assert user.id == user_id and login.id == sid and session is None
        finally:
            event.remove(engine, "before_cursor_execute", before)
            event.remove(engine, "handle_error", failed)
            await reader.close()
    async def bounded():
        # Overall test anti-hang, never passed as a runtime authorization budget.
        async with asyncio.timeout(5):
            await run()
    app_client.portal.call(bounded)
