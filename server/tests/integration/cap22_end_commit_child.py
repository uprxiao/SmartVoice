"""G2 end COMMIT observation; actual G1 server and cancellation stay intact."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import sys
import time

from sqlalchemy.ext.asyncio import AsyncSession
from app import main
from app.models import VoiceSession
from app.providers.asr.mock import MockAsrSession
from app.services.session_runtime import RuntimeSession, SessionManager
from tests.integration import cap22_sigterm_child as g1


def run(folder, fd):
    def save(name, value):
        temporary = folder / (name + ".pending")
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f:
            json.dump(value, f, sort_keys=True)
        os.link(temporary, folder / name)
        temporary.unlink()

    scope = ContextVar("owned_end_commit_scope", default=None)
    actual_end, actual_commit = SessionManager._accept_end, AsyncSession.commit
    actual_finish, actual_emit = MockAsrSession.finish, RuntimeSession.emit
    actual_cutoff = main._begin_instance_shutdown
    observation = {"used": False, "commit_completed": False, "gate_cancelled": False,
        "events": [], "finishes": [], "cutoff": None}
    target_runtime = None

    def target():
        path = folder / "end-target.json"
        return json.loads(path.read_text()) if path.is_file() else None

    async def accept_end(manager, runtime, last_seq, total_samples):
        nonlocal target_runtime
        selection = target()
        if not selection or runtime.session_id != selection["session_id"]:
            return await actual_end(manager, runtime, last_seq, total_samples)
        assert runtime.user_id == selection["user_id"] and runtime.samples == total_samples == 4000
        target_runtime = runtime
        token = scope.set((manager, runtime, selection))
        try:
            return await actual_end(manager, runtime, last_seq, total_samples)
        finally:
            observation["accept_return"] = {"at": time.monotonic(), "cancelled": runtime.cancelled,
                "state": runtime.state, "ended": runtime.ended,
                "processing_accepted": runtime.processing_accepted,
                "exception_type": None if sys.exc_info()[0] is None else sys.exc_info()[0].__name__}
            scope.reset(token)

    async def commit(db):
        context = scope.get()
        if context is None or observation["used"]:
            return await actual_commit(db)
        manager, runtime, selection = context
        rows = [row for row in db.identity_map.values() if isinstance(row, VoiceSession)
            and row.user_id == runtime.user_id and row.id == runtime.session_id]
        assert len(rows) == 1 and rows[0].state == "finalizing" and rows[0].audio_complete
        observation["used"] = True
        if selection["stage"] == "after_commit_before_publish":
            await actual_commit(db)
            observation["commit_completed"] = True
        else:
            assert selection["stage"] == "before_commit"
        save("end-gate-entered.json", {"stage": selection["stage"], "session_id": runtime.session_id,
            "committed": observation["commit_completed"], "in_transaction": db.in_transaction(),
            "runtime_state": runtime.state, "processing_accepted": runtime.processing_accepted,
            "ended": runtime.ended, "end_ack_emitted": "audio.end_ack" in observation["events"],
            "operation_count": len(manager._operations), "pcm_bytes": len(runtime.audio), "at": time.monotonic()})
        try:
            async with asyncio.timeout(8):
                while not (folder / "end-release").is_file():
                    await asyncio.sleep(.005)
        except asyncio.CancelledError:
            # Sender completion may cancel the receiver after cutoff. Observe,
            # do not suppress this real cancellation or force a cancelled COMMIT.
            observation["gate_cancelled"] = True
            save("end-gate-cancelled.json", {"committed": observation["commit_completed"],
                "closing": manager._closing, "at": time.monotonic()})
            raise
        except TimeoutError:
            save("end-control-failure.json", {"type": "TimeoutError", "phase": "commit_gate"})
            raise
        save("end-gate-release.json", {"closing": manager._closing, "at": time.monotonic()})
        if not observation["commit_completed"]:
            await actual_commit(db)
            observation["commit_completed"] = True

    def emitted(runtime, kind, **data):
        if runtime is target_runtime:
            observation["events"].append(kind)
        return actual_emit(runtime, kind, **data)

    async def finished(stream, total_samples, deadline):
        observation["finishes"].append({"session_id": stream.options.session_id,
            "attempt_no": stream.options.attempt_no, "samples": total_samples,
            "remaining_seconds": deadline - asyncio.get_running_loop().time()})
        return await actual_finish(stream, total_samples, deadline)

    def cutoff(app):
        if observation["cutoff"] is None:
            observation["cutoff"] = {"at": time.monotonic(), "committed": observation["commit_completed"],
                "target_state": target_runtime.state if target_runtime else None,
                "processing_accepted": target_runtime.processing_accepted if target_runtime else None}
        return actual_cutoff(app)  # One normal production call; no preemptive cutoff.

    SessionManager._accept_end, AsyncSession.commit = accept_end, commit
    RuntimeSession.emit, MockAsrSession.finish = emitted, finished
    main._begin_instance_shutdown = cutoff
    try:
        g1.run(folder, fd)
    finally:
        observation["target_ref"] = None if target_runtime is None else {
            "pcm_bytes": len(target_runtime.audio), "asr_attached": target_runtime.asr is not None,
            "processing_done": target_runtime.task is None or target_runtime.task.done()}
        save("end-observer-final.json", observation)


if __name__ == "__main__":
    run(Path(sys.argv[1]), int(sys.argv[2]))
