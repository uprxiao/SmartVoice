"""Deployment pool-timeout contract on the process-owned real PostgreSQL DB.

No application lifespan/model/session traffic or product deadline is changed.
The synthetic route only exposes a real checkout failure to the application's
existing SQLAlchemyError handler; it is not a new product/API endpoint.
"""
from __future__ import annotations

import asyncio
import json
import math

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError, TimeoutError as PoolTimeout

from app.core.config import DatabaseConfig, load_config
from app.core.db import Database
from tests.conftest import SERVER_ROOT, TEST_DB


def test_default_engine_pool_timeout_matches_deployment_two_seconds(test_database, record_property):
    async def run():
        config = load_config(SERVER_ROOT / 'config' / 'app.yaml')
        assert make_url(config.database_url).database == TEST_DB
        database = Database(config)
        evidence = {'actual_engine_timeout_seconds': database.engine.pool.timeout(),
                    'expected_deployment_timeout_seconds': 2,
                    'pool_size': database.engine.pool.size(),
                    'max_overflow': database.engine.pool._max_overflow,
                    'engine_disposed': False}
        try:
            # Inspect the actual application's engine, not only a config value
            # or mock create_async_engine call. Baseline SQLAlchemy default=30.
            assert database.engine.pool.timeout() == 2
            assert database.engine.pool.size() == 5
            assert database.engine.pool._max_overflow == 5
        finally:
            await database.close()
            evidence['engine_disposed'] = True
            record_property('default_pool_timeout_contract', json.dumps(evidence))
    asyncio.run(run())


def test_explicit_pool_timeout_real_pg_recovers_and_returns_safe_503(
    test_database, tmp_path, record_property,
):
    async def run():
        configured = .2
        # Baseline must reject this previously missing declared field, before
        # any held connection. The same unchanged test then exercises real PG.
        path = tmp_path / 'pool-timeout.yaml'
        path.write_text('environment: development\ndatabase:\n  pool_size: 1\n'
                        '  max_overflow: 0\n  pool_timeout_seconds: 0.2\n')
        config = load_config(path)
        assert make_url(config.database_url).database == TEST_DB
        assert config.database.pool_timeout_seconds == configured
        for invalid in (0, -1, math.inf, -math.inf, math.nan):
            with pytest.raises(ValidationError):
                DatabaseConfig(pool_timeout_seconds=invalid)
        database = Database(config)
        engine = database.engine
        evidence = {'configured_seconds': configured,
                    'actual_engine_timeout_seconds': engine.pool.timeout(),
                    'held_real_pg_lease': False, 'pool_timeout_observed': False,
                    'asgi_safe_503_observed': False, 'next_real_query_succeeded': False,
                    'technical_observation_guard_seconds': 3,
                    'observation_guard_is_product_SLA': False,
                    'checkout_count': 0, 'checkin_count': 0,
                    'remaining_checkedout': None, 'engine_disposed': False}
        held = None
        def checkout(dbapi_connection, connection_record, connection_proxy):
            evidence['checkout_count'] += 1
        def checkin(dbapi_connection, connection_record):
            evidence['checkin_count'] += 1
        event.listen(engine.sync_engine.pool, 'checkout', checkout)
        event.listen(engine.sync_engine.pool, 'checkin', checkin)
        try:
            assert engine.pool.timeout() == configured
            assert engine.pool.size() == 1 and engine.pool._max_overflow == 0
            held = await engine.connect()
            assert await held.scalar(text('SELECT current_database()')) == TEST_DB
            assert engine.pool.checkedout() == 1
            evidence['held_real_pg_lease'] = True
            loop = asyncio.get_running_loop()
            started = loop.time()
            # A harness anti-hang observer, not a larger checkout allowance.
            # The real .2s pool timeout must raise the original SQLAlchemy type.
            async with asyncio.timeout(3):
                with pytest.raises(PoolTimeout):
                    async with engine.connect():
                        pytest.fail('saturated single-slot pool unexpectedly admitted another lease')
            evidence['pool_timeout_elapsed_seconds'] = loop.time() - started
            assert evidence['pool_timeout_elapsed_seconds'] >= configured
            evidence['pool_timeout_observed'] = True
            assert engine.pool.checkedout() == 1
            assert evidence['checkout_count'] == 1 and evidence['checkin_count'] == 0

            from app.main import create_app
            app = create_app(str(path))
            # No lifespan/health/model startup: retain the production exception
            # handler and HttpGuard, exercise it via one declared owned route.
            app.state.db = database
            app.state.database_ok = app.state.ready = True
            app.state.instance_fenced = app.state.stopping = False
            assert SQLAlchemyError in app.exception_handlers
            @app.get('/__owned_pool_timeout_probe')
            async def owned_probe():
                async with database.session_factory() as session:
                    return {'value': await session.scalar(text('SELECT 1'))}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url='http://testserver',
            ) as client:
                async with asyncio.timeout(3):
                    response = await client.get('/__owned_pool_timeout_probe')
                assert response.status_code == 503
                error = response.json()['error']
                safe_code = error.get('code') == 'SERVICE_UNAVAILABLE' and error.get('retryable') is True
                safe_details = error.get('details') == {} and bool(error.get('request_id'))
                assert safe_code, 'pool failure must use the existing safe 503 code'
                assert safe_details, 'pool failure must not return diagnostic details'
                assert response.headers['x-request-id'] == error['request_id']
                assert set(error) == {'code', 'message', 'request_id', 'retryable', 'details'}
                rendered = response.text
                url = make_url(config.database_url)
                safe_text = 'Traceback' not in rendered and 'SELECT 1' not in rendered
                no_url = config.database_url not in rendered
                no_password = url.password is None or url.password not in rendered
                assert safe_text and no_url and no_password, 'no exception, SQL, database URL or credential in response'
                assert engine.pool.checkedout() == 1
                assert evidence['checkout_count'] == 1 and evidence['checkin_count'] == 0
                evidence['asgi_safe_503_observed'] = True
                # Only the real holder's close releases this slot. No forced
                # reset, dispose/replacement engine or retry loop creates room.
                await held.close()
                held = None
                assert engine.pool.checkedout() == 0
                assert evidence['checkin_count'] == 1
                async with asyncio.timeout(3):
                    recovered = await client.get('/__owned_pool_timeout_probe')
                assert recovered.status_code == 200 and recovered.json() == {'value': 1}
                evidence['next_real_query_succeeded'] = True
            assert engine.pool.checkedout() == 0
            assert evidence['checkout_count'] == evidence['checkin_count'] == 2
        finally:
            if held is not None:
                await held.close()
            evidence['remaining_checkedout'] = engine.pool.checkedout()
            event.remove(engine.sync_engine.pool, 'checkout', checkout)
            event.remove(engine.sync_engine.pool, 'checkin', checkin)
            await database.close()
            evidence['engine_disposed'] = True
            record_property('explicit_pool_timeout_real_pg', json.dumps(evidence))
        assert evidence['remaining_checkedout'] == 0 and evidence['engine_disposed']
    asyncio.run(run())
