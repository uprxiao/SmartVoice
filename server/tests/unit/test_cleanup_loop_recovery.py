"""Worker recovery only; not real-PG read barriers or statistics rebuild proof."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.services import cleanup


PHASES = ("connect_timeouts", "retry_audio", "history")
FUNCTIONS = ("cleanup_connect_timeouts", "cleanup_retry_audio", "cleanup_history")
PRIVATE_CANARY = "synthetic-private-body postgresql://user:secret@host/db"


class TickClock:
    """Replace only cleanup's asyncio binding, never the shared asyncio module."""

    def __init__(self, ticks, *, cancel_in_sleep=False):
        self.ticks = ticks
        self.now = 10_000.0
        self.sleeps = []
        self.cancel_in_sleep = cancel_in_sleep

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        assert seconds == 1, "Recovery must retain the bounded one-second tick"
        if self.cancel_in_sleep or len(self.sleeps) > self.ticks:
            raise asyncio.CancelledError
        self.now += seconds

    def time(self):
        return self.now

    def install(self, monkeypatch):
        monkeypatch.setattr(cleanup, "asyncio", SimpleNamespace(
            sleep=self.sleep,
            get_running_loop=lambda: self,
            CancelledError=asyncio.CancelledError,
        ))


def run_worker(monkeypatch, clock, jobs, *, history_interval=3600):
    clock.install(monkeypatch)
    for name, job in zip(FUNCTIONS, jobs, strict=True):
        monkeypatch.setattr(cleanup, name, job)
    app = SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace(
        cleanup=SimpleNamespace(history_interval_seconds=history_interval),
    )))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cleanup.cleanup_loop(app))


@pytest.mark.parametrize("failed_phase", PHASES)
def test_one_failure_recovers_next_tick_without_stopping_other_jobs(
    monkeypatch, caplog, failed_phase,
):
    clock = TickClock(4)
    calls = {phase: [] for phase in PHASES}

    def job_for(phase):
        async def job(_app):
            calls[phase].append(clock.now)
            if phase == failed_phase and len(calls[phase]) == 1:
                raise RuntimeError(PRIVATE_CANARY)
            return 0
        return job

    with caplog.at_level(logging.WARNING, logger="smartvoiceinput.cleanup"):
        run_worker(monkeypatch, clock, [job_for(phase) for phase in PHASES])

    assert calls["connect_timeouts"] == [10001, 10002, 10003, 10004]
    assert calls["retry_audio"] == [10001, 10002, 10003, 10004]
    assert calls["history"] == ([10001, 10002] if failed_phase == "history" else [10001])
    assert clock.sleeps == [1] * 5
    records = [record for record in caplog.records if record.name == "smartvoiceinput.cleanup"]
    assert len(records) == 1
    record = records[0]
    assert record.code == "CLEANUP_JOB_FAILED"
    assert record.phase == failed_phase and record.exception_type == "RuntimeError"
    assert record.getMessage() == f"CLEANUP_JOB_FAILED phase={failed_phase} exception_type=RuntimeError"
    assert record.exc_info is None and record.stack_info is None
    assert PRIVATE_CANARY not in caplog.text and "secret" not in caplog.text


def test_history_interval_starts_after_success_and_initial_run_is_due(monkeypatch):
    clock = TickClock(7)
    clock.now = 0
    history_calls = []

    async def ordinary_job(_app):
        return 0

    async def history_job(_app):
        history_calls.append(clock.now)
        # Simulate a nonzero-duration successful pass without real sleeping.
        clock.now += 2
        return 0

    run_worker(monkeypatch, clock, [ordinary_job, ordinary_job, history_job], history_interval=5)
    assert history_calls == [1, 8]
    assert clock.sleeps == [1] * 8


@pytest.mark.parametrize("failed_phase", PHASES)
def test_repeated_failure_is_tick_bounded_and_error_logging_is_rate_limited(
    monkeypatch, caplog, failed_phase,
):
    clock = TickClock(130)
    calls = {phase: 0 for phase in PHASES}

    def job_for(phase):
        async def job(_app):
            calls[phase] += 1
            if phase == failed_phase:
                raise RuntimeError(PRIVATE_CANARY)
            return 0
        return job

    with caplog.at_level(logging.WARNING, logger="smartvoiceinput.cleanup"):
        run_worker(monkeypatch, clock, [job_for(phase) for phase in PHASES])
    assert calls["connect_timeouts"] == calls["retry_audio"] == 130
    assert calls["history"] == (130 if failed_phase == "history" else 1)
    assert clock.sleeps == [1] * 131
    records = [record for record in caplog.records if record.name == "smartvoiceinput.cleanup"]
    assert len(records) == 3  # first failure, +60 s, +120 s
    assert all(record.phase == failed_phase and record.code == "CLEANUP_JOB_FAILED" for record in records)
    assert PRIVATE_CANARY not in caplog.text


def test_success_resets_failure_log_suppression(monkeypatch, caplog):
    clock = TickClock(3)
    attempts = 0

    async def intermittent(_app):
        nonlocal attempts
        attempts += 1
        if attempts in (1, 3):
            raise ValueError(PRIVATE_CANARY)
        return 0

    async def okay(_app):
        return 0

    with caplog.at_level(logging.WARNING, logger="smartvoiceinput.cleanup"):
        run_worker(monkeypatch, clock, [intermittent, okay, okay])
    records = [record for record in caplog.records if record.name == "smartvoiceinput.cleanup"]
    assert len(records) == 2
    assert all(record.exception_type == "ValueError" for record in records)


@pytest.mark.parametrize("cancel_phase", PHASES)
def test_job_cancellation_propagates_immediately(monkeypatch, caplog, cancel_phase):
    clock = TickClock(5)
    visited = []

    def job_for(phase):
        async def job(_app):
            visited.append(phase)
            if phase == cancel_phase:
                raise asyncio.CancelledError
            return 0
        return job

    run_worker(monkeypatch, clock, [job_for(phase) for phase in PHASES])
    assert visited == list(PHASES[:PHASES.index(cancel_phase) + 1])
    assert clock.sleeps == [1]
    assert not [record for record in caplog.records if record.name == "smartvoiceinput.cleanup"]


def test_sleep_cancellation_does_not_run_a_job(monkeypatch, caplog):
    clock = TickClock(5, cancel_in_sleep=True)
    visited = []

    async def job(_app):
        visited.append(True)
        return 0

    run_worker(monkeypatch, clock, [job, job, job])
    assert visited == [] and clock.sleeps == [1]
    assert not [record for record in caplog.records if record.name == "smartvoiceinput.cleanup"]
