"""Pure helper unit coverage; no HTTP, database, provider or business deadline."""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import weakref
from types import SimpleNamespace

import pytest

from app.services import safe_exception_metadata as safe

BASE_KEYS = {"event", "phase", "code", "session_id", "attempt_no", "exception_type", "frames"}
CHAIN_KEYS = {"exception_chain", "exception_chain_truncated",
              "exception_chain_cycle_detected", "exception_chain_unavailable"}
CANARY = "PRIVATE_SQL_PARAM_PASSWORD_BODY_7e8c6b35"
SID = "00000001-0000-4000-8000-000000000000"


def _records(caplog):
    return [record for record in caplog.records if record.name == safe._LOGGER.name]


def _emit(caplog, error, phase="watchdog_auth_read"):
    caplog.set_level(logging.WARNING, logger=safe._LOGGER.name)
    safe.log_exception_metadata(error, phase=phase, code="INTERNAL_ERROR",
                                session_id=SID, attempt_no=1)
    records = _records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.args == () and record.exc_info is None and record.stack_info is None
    payload = json.loads(record.getMessage())
    assert payload["event"] == "RUNTIME_SAFE_EXCEPTION" and payload["phase"] == phase
    assert payload["code"] == "INTERNAL_ERROR" and payload["session_id"] == SID
    assert payload["attempt_no"] == 1
    return payload, record


@pytest.mark.parametrize("mode", ["timeout", "wait_for"])
def test_auth_real_timeout_retains_actual_cancelled_wait_frames(caplog, mode):
    async def exercise():
        entered = asyncio.Event()

        async def actual_wait_point():
            entered.set()
            await asyncio.Event().wait()

        try:
            if mode == "timeout":
                async with asyncio.timeout(0):
                    await actual_wait_point()
            else:
                task = asyncio.create_task(actual_wait_point())
                await entered.wait()  # The actual task is suspended at Event.wait.
                await asyncio.wait_for(task, timeout=0)
        except TimeoutError as error:
            assert isinstance(error.__cause__, asyncio.CancelledError)
            original_cause, original_traceback = error.__cause__, error.__traceback__
            payload, _ = _emit(caplog, error)
            assert error.__cause__ is original_cause and error.__traceback__ is original_traceback
            assert set(payload) == BASE_KEYS | CHAIN_KEYS
            first = payload["exception_chain"][0]
            assert first["relation"] == "cause" and first["exception_type"] == "CancelledError"
            assert any(frame["basename"] == "locks.py" and frame["function"] == "wait"
                       for frame in first["frames"])
            assert any(frame["function"] == "actual_wait_point" for frame in first["frames"])
            assert not payload["exception_chain_unavailable"]
        else:
            pytest.fail("real timeout did not happen")

    asyncio.run(exercise())


@pytest.mark.parametrize("phase,kind", [
    ("process_sqlalchemy", "sql_like"),
    ("process_sqlalchemy", "timeout"),
    ("auth_fetch_failed", "other"),
])
def test_other_payloads_keep_original_exact_seven_fields_and_one_log(caplog, phase, kind):
    class SqlErrorLike(Exception):
        pass

    error = TimeoutError(CANARY) if kind == "timeout" else SqlErrorLike(CANARY)
    error.__cause__ = ValueError(CANARY)
    payload, record = _emit(caplog, error, phase)
    assert set(payload) == BASE_KEYS
    assert CANARY not in json.dumps(record.__dict__)


@pytest.mark.parametrize("selection", ["cause_wins", "context", "suppressed_context"])
def test_chain_uses_explicit_cause_or_only_unsuppressed_context(caplog, selection):
    error = TimeoutError(CANARY)
    error.__context__ = LookupError(CANARY)
    error.__suppress_context__ = selection == "suppressed_context"
    if selection == "cause_wins":
        error.__cause__ = ValueError(CANARY)
        error.__suppress_context__ = False
    payload, _ = _emit(caplog, error, "auth_batch_failed")
    chain = payload["exception_chain"]
    if selection == "suppressed_context":
        assert chain == []
    else:
        assert len(chain) == 1
        assert (chain[0]["relation"], chain[0]["exception_type"]) == (
            ("cause", "ValueError") if selection == "cause_wins" else ("context", "LookupError"))
    assert not payload["exception_chain_cycle_detected"]
    assert not payload["exception_chain_truncated"]


@pytest.mark.parametrize("shape", ["depth", "cycle"])
def test_chain_depth_and_identity_cycle_are_bounded(caplog, shape):
    error = TimeoutError(CANARY)
    causes = [ValueError(CANARY) for _ in range(4)]
    error.__cause__ = causes[0]
    if shape == "depth":
        for left, right in zip(causes, causes[1:]):
            left.__cause__ = right
    else:
        causes[0].__cause__ = causes[1]
        causes[1].__cause__ = causes[0]
    payload, _ = _emit(caplog, error)
    assert len(payload["exception_chain"]) == (3 if shape == "depth" else 2)
    assert payload["exception_chain_truncated"] is (shape == "depth")
    assert payload["exception_chain_cycle_detected"] is (shape == "cycle")


def _capture_stack(depth):
    def original_wait_leaf():
        raise LookupError(CANARY)

    def descend(remaining):
        if remaining:
            return descend(remaining - 1)
        return original_wait_leaf()

    try:
        descend(depth)
    except LookupError as error:
        return error


@pytest.mark.parametrize("depth,scan_truncated", [(12, False), (80, True)])
def test_chain_preserves_visited_tail_eight_and_marks_unvisited_traceback(caplog, depth, scan_truncated):
    error = TimeoutError(CANARY)
    error.__cause__ = _capture_stack(depth)
    payload, _ = _emit(caplog, error)
    entry = payload["exception_chain"][0]
    assert len(entry["frames"]) == 8 and entry["frames_truncated"] is True
    assert entry["traceback_scan_truncated"] is scan_truncated
    assert any(frame["function"] == "original_wait_leaf" for frame in entry["frames"]) is (not scan_truncated)
    assert all(frame["function"] != "_capture_stack" for frame in entry["frames"])
    assert payload["frames"] == []  # Existing outer traceback field is unchanged.


def test_private_messages_attributes_paths_and_locals_never_enter_record(caplog):
    class Poison(Exception):
        def __str__(self):
            raise AssertionError("str must not be called")

        def __repr__(self):
            raise AssertionError("repr must not be called")

        def __getattribute__(self, name):
            if name in {"args", "params", "statement", "orig", "sql"}:
                raise AssertionError("private attribute must not be read")
            return super().__getattribute__(name)

    namespace = {"Poison": Poison, "CANARY": CANARY}
    exec(compile("def 等待():\n    private_local = CANARY\n    raise Poison(CANARY)\n",
                 "/private/" + CANARY + "/bad?trace.py", "exec"), namespace)
    try:
        namespace["等待"]()
    except Poison as cause:
        error = TimeoutError(CANARY)
        error.__cause__ = cause
        payload, record = _emit(caplog, error)
    serialized = json.dumps(record.__dict__)
    assert CANARY not in serialized  # Includes the private synthetic traceback directory.
    assert any(frame["basename"] == frame["function"] == "unknown"
               for frame in payload["exception_chain"][0]["frames"])
    assert all("/" not in frame["basename"] and "\\" not in frame["basename"]
               for entry in payload["exception_chain"] for frame in entry["frames"])


def test_emitted_metadata_retains_no_original_traceback_local_object(caplog):
    class LocalObject:
        pass

    def capture():
        private_local = LocalObject()
        reference = weakref.ref(private_local)
        try:
            raise ValueError(CANARY)
        except ValueError as caught:
            return caught, reference

    cause, reference = capture()
    error = TimeoutError(CANARY)
    error.__cause__ = cause
    _emit(caplog, error)
    cause.__traceback__ = None
    error.__cause__ = error.__context__ = None
    del cause, error
    gc.collect()
    assert reference() is None


def test_broken_logger_cannot_replace_original_timeout_or_mutate_its_chain(monkeypatch):
    calls = []

    def broken(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("fixed logger failure")

    monkeypatch.setattr(safe._LOGGER, "warning", broken)
    error = TimeoutError(CANARY)
    cause = ValueError(CANARY)
    error.__cause__ = cause
    try:
        raise error
    except TimeoutError as original:
        traceback = original.__traceback__
        safe.log_exception_metadata(original, phase="watchdog_auth_read")
        assert original is error and original.__cause__ is cause
        assert original.__traceback__ is traceback
    assert len(calls) == 1 and isinstance(calls[0][0][0], str)
    assert CANARY not in calls[0][0][0]


@pytest.mark.parametrize("waiter_count,expected_logs", [(0, 1), (10, 8)])
def test_batch_label_limit_and_fallback_keep_original_log_count(caplog, waiter_count, expected_logs):
    caplog.set_level(logging.WARNING, logger=safe._LOGGER.name)
    error = TimeoutError(CANARY)
    error.__cause__ = asyncio.CancelledError(CANARY)
    waiters = [SimpleNamespace(key=SimpleNamespace(
        session_id=f"{index:08x}-0000-4000-8000-000000000000")) for index in range(waiter_count)]
    safe.log_batch_exception_metadata(error, phase="auth_fetch_failed", waiters=waiters)
    records = _records(caplog)
    assert len(records) == expected_logs
    for record in records:
        assert record.args == () and record.exc_info is None and record.stack_info is None
        assert CANARY not in json.dumps(record.__dict__)
        assert json.loads(record.getMessage())["exception_chain"][0]["exception_type"] == "CancelledError"
