"""Identifiers only: provider tasks must never put prompts/audio in telemetry."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CallScope:
    user_id: str
    session_id: str | None = None
    attempt_no: int | None = None
    learning_event_id: str | None = None
    synthetic: bool = False


# Existing maximum metadata-write allowance. LLM dispatch reserves it INSIDE
# the caller's absolute deadline; it is never extra provider or retry time.
OUTCOME_WRITE_BUDGET_SECONDS = 1.0

current_scope: ContextVar[CallScope | None] = ContextVar("inference_scope", default=None)


@contextmanager
def call_scope(scope: CallScope):
    token = current_scope.set(scope)
    try:
        yield
    finally:
        current_scope.reset(token)


class CallRecorder(Protocol):
    async def started(self, scope: CallScope, provider: str, model_id: str,
                      purpose: str, processed_audio_samples: int = 0) -> str: ...
    async def finished(self, call_id: str, status: str, error_code: str | None,
                       elapsed_ms: int, prompt_tokens: int | None = None,
                       completion_tokens: int | None = None) -> None: ...


async def persist_outcome(recorder, outcome: dict, deadline: float) -> bool:
    """Join one metadata writer without lending it more than the original budget.

    Caller cancellation is deferred until the writer has finished or rolled
    back; it never detaches a write or cuts that cleanup short a second time.
    Cancellation/rollback itself remains cooperative and can outlast the write
    deadline. Such a completion is unconfirmed, never reported as success.
    """
    import asyncio
    import logging
    from ..core.clock import loop_time
    if recorder is None or not outcome.get("call_id"):
        return True
    write_deadline = min(deadline, loop_time() + OUTCOME_WRITE_BUDGET_SECONDS)
    # The protocol contains scalar identifiers/counts only. Freeze both the
    # payload and bound recorder method before publishing the sole writer task.
    frozen = dict(outcome)
    warned = False

    def unconfirmed():
        nonlocal warned
        if not warned:
            warned = True
            logging.getLogger(__name__).warning(
                "MODEL_CALL_OUTCOME_UNCONFIRMED call_id=%s", frozen["call_id"])

    if loop_time() >= write_deadline:
        unconfirmed()
        return False
    try:
        finished = recorder.finished
    except Exception:
        unconfirmed()
        return False

    async def write():
        try:
            if loop_time() >= write_deadline:
                return False  # A delayed task must not start a late DB write.
            async with asyncio.timeout_at(write_deadline):
                await finished(**frozen)
            # A recorder suppressing timeout cancellation cannot manufacture a
            # successful in-budget outcome by returning after the deadline.
            return loop_time() <= write_deadline
        except (asyncio.CancelledError, Exception):
            return False

    caller = asyncio.current_task()
    initial_cancels = caller.cancelling() if caller is not None else 0
    writer = asyncio.create_task(write(), name="svi-model-outcome-write")
    # Observe an overdue cancellation/rollback immediately, even if driver
    # cleanup has not returned. This timer ONLY logs; timeout_at owns the one
    # deadline cancellation, so it cannot interrupt rollback a second time.
    def warn_if_pending():
        if not writer.done():
            unconfirmed()
    warning_timer = asyncio.get_running_loop().call_at(write_deadline, warn_if_pending)
    interrupted = None
    try:
        while not writer.done():
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError as exc:
                # An independently cancelled writer is an unconfirmed write,
                # not a new caller cancellation. Preserve the first real caller
                # cancellation (including any outer timeout's cancellation).
                if (not writer.cancelled() or
                        (caller is not None and caller.cancelling() > initial_cancels)):
                    if interrupted is None:
                        interrupted = exc
        recorded = False if writer.cancelled() else writer.result()
    finally:
        warning_timer.cancel()
    if not recorded:
        unconfirmed()
    if interrupted is not None:
        raise interrupted
    return recorded
