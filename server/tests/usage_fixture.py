"""F-USAGE accounting fixtures only; never evidence of actual model throughput.

Keep these helpers under tests. Explicit fixture executions use the production
recorder/aggregator, with valid tenant-owned parents. No model_calls or aggregate
rows are added to production providers to satisfy assertions.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import datetime as dt
import hashlib
import time

from sqlalchemy import select

from app.core.security import idempotency_hash
from app.models import Insertion, LearningEvent, LoginSession, ModelCall, SessionAttempt, SessionResult, VoiceSession
from app.providers.asr.mock import MockAsrProvider, MockAsrSession
from app.services import stats_service
from app.services.inference_context import CallScope
from tests.ids import fixture_uuid


@dataclass(frozen=True)
class UsageFixture:
    user_id: str
    session_id: str
    request_label: str
    result_id: str
    learning_event_id: str
    call_ids: tuple[str, ...]


def seed_f_usage(app_client, user: dict, day: str, label: str) -> UsageFixture:
    """Seed fixed F-USAGE facts then use recorder.finished to build daily usage.

    Parents are explicit database fixtures, not a pretend successful dictation.
    The stored create hash permits a real HTTP idempotent replay without invoking
    any provider. No active runtime/capacity reservation is manufactured.
    """
    owner = user["user"]["id"]
    session_id, result_id, insertion_id, event_id = (fixture_uuid(label + suffix)
        for suffix in ("-session", "-result", "-insertion", "-learning"))
    request_label = label + "-create"
    request_id = fixture_uuid(request_label)
    started = dt.datetime.fromisoformat(day + "T10:00:00+00:00")
    ended = started + dt.timedelta(seconds=3)
    text = "SQL lite API"
    text_hash = hashlib.sha256(text.encode()).hexdigest()

    async def parents():
        state = app_client.app.state
        async with state.db.session_factory() as db:
            login = await db.scalar(select(LoginSession).where(
                LoginSession.user_id == owner, LoginSession.client_type == "desktop",
                LoginSession.device_id.is_not(None), LoginSession.revoked_at.is_(None)
            ).order_by(LoginSession.created_at.desc()).limit(1))
            assert login is not None, "F-USAGE needs an existing tenant-owned Desktop login"
            session = VoiceSession(id=session_id, user_id=owner, device_id=login.device_id,
                created_login_sid=login.id, client_request_id=request_id,
                request_hash=idempotency_hash(state.config.idempotency_secret, "session.create", {
                    "client_request_id": request_id, "mode": "dictation", "learning_enabled": True}),
                state="ready", current_attempt_no=1, dictionary_version=0, learning_enabled=True,
                received_samples=48000, audio_complete=True, created_at=started,
                end_accepted_at=ended, first_terminal_at=ended, asr_hints=["SQL lite"], llm_hints=["SQL lite"])
            db.add(session)
            await db.flush()
            db.add(SessionAttempt(id=fixture_uuid(label + "-attempt"), user_id=owner,
                session_id=session_id, attempt_no=1, kind="initial", state="ready",
                raw_text=text, started_at=started, ended_at=ended))
            await db.flush()
            db.add(SessionResult(id=result_id, user_id=owner, session_id=session_id, attempt_no=1,
                raw_text=text, refined_text=text, review_required=False, text_hash=text_hash,
                dictionary_version=0, char_count=10, created_at=ended))
            await db.flush()
            session.current_result_id = result_id
            db.add(Insertion(id=insertion_id, user_id=owner, session_id=session_id,
                result_id=result_id, device_id=login.device_id, text_hash=text_hash,
                status="inserted", observable=True, request_hash=bytes(32),
                claimed_at=ended, completed_at=ended))
            await db.flush()
            db.add(LearningEvent(id=event_id, user_id=owner, session_id=session_id,
                insertion_id=insertion_id, correction_id=fixture_uuid(label + "-correction"),
                request_hash=bytes(32), offset=0, before_text="SQL lite", after_text="SQLite",
                left_context="", right_context=" API", status="suggested", classification="suggest",
                ambiguous=True, created_at=ended + dt.timedelta(seconds=1)))
            await stats_service.record_session(db, session, "ready", has_result=True, char_count=10)
            await db.flush()
            await stats_service.record_inserted(db, owner, session_id)
            await db.commit()

    app_client.portal.call(parents)

    async def calls():
        state = app_client.app.state
        identities = []
        # These are declared F-USAGE aggregation fixtures. A failed local request
        # with unavailable token usage is deliberately different from zero tokens.
        facts = (
            ("asr_decode", "succeeded", 16000, None, None),
            ("asr_decode", "succeeded", 32000, None, None),
            ("asr_finish", "succeeded", 48000, None, None),
            ("refine", "failed", 0, None, None),
            ("refine", "succeeded", 0, 100, 20),
            ("audit", "succeeded", 0, 40, 5),
            ("classify", "succeeded", 0, 30, 3),
        )
        for index, (purpose, status, samples, prompt_tokens, completion_tokens) in enumerate(facts):
            scope = CallScope(owner, session_id, 1, event_id if purpose == "classify" else None)
            call_id = await state.recorder.started(scope, "test-usage-fixture",
                "f-usage-asr" if purpose.startswith("asr_") else "f-usage-llm", purpose, samples)
            assert call_id, "a declared fixture call must have its own durable identity"
            # Fixed historical business day is data, not a global clock monkeypatch.
            async with state.db.session_factory() as db:
                row = await db.get(ModelCall, call_id)
                assert row is not None and row.status == "running" and row.aggregated_at is None
                row.started_at = started + dt.timedelta(milliseconds=index * 10)
                await db.commit()
            await state.recorder.finished(call_id, status,
                "F_USAGE_TIMEOUT_FIXTURE" if status == "failed" else None,
                10, prompt_tokens, completion_tokens)
            identities.append(call_id)
        return tuple(identities)

    call_ids = app_client.portal.call(calls)
    assert len(set(call_ids)) == 7
    return UsageFixture(owner, session_id, request_label, result_id, event_id, call_ids)


class InstrumentedFixtureAsrSession(MockAsrSession):
    """Record only when this deterministic test implementation actually executes."""
    def __init__(self, options, provider):
        super().__init__(options, provider.text)
        self.provider = provider

    async def finish(self, total_samples: int, deadline: float):
        if self.cancelled:
            raise asyncio.CancelledError()
        scope = CallScope(self.options.user_id, self.options.session_id, self.options.attempt_no)
        call_id = await self.provider.recorder.started(scope, "test-instrumented-asr",
            self.provider.model_id, "asr_finish", total_samples)
        assert call_id
        started = time.monotonic()
        status, error = "failed", "FIXTURE_EXECUTION_FAILED"
        try:
            self.provider.executions.append((call_id, self.options.session_id, total_samples))
            final = await super().finish(total_samples, deadline)
            status, error = "succeeded", None
            return replace(final, model_id=self.provider.model_id)
        except asyncio.CancelledError:
            status, error = "cancelled", "FIXTURE_CANCELLED"
            raise
        finally:
            await self.provider.recorder.finished(call_id, status, error,
                int((time.monotonic() - started) * 1000))


class InstrumentedFixtureAsrProvider(MockAsrProvider):
    model_id = "test-instrumented-asr"

    def __init__(self, recorder):
        super().__init__()
        self.recorder = recorder
        self.executions: list[tuple[str, str, int]] = []

    async def open(self, options):
        return InstrumentedFixtureAsrSession(options, self)


def install_instrumented_fixture_asr(app_client) -> InstrumentedFixtureAsrProvider:
    async def install():
        state = app_client.app.state
        assert not state.sessions.runtimes, "install the test provider before creating sessions"
        provider = InstrumentedFixtureAsrProvider(state.recorder)
        state.asr = state.sessions.asr_provider = provider
        return provider
    return app_client.portal.call(install)
