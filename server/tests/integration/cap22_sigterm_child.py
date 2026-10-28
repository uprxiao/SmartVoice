"""Test child only: official Uvicorn/app, declared Mock ASR/refine gates."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time

import uvicorn
from app import main
from app.providers.asr.base import ProviderError
from app.providers.asr.mock import MockAsrSession
from app.providers.llm.mock import MockLlmProvider
from app.services.inference_context import current_scope


def run(folder: Path, fd: int):
    def save(name, data):
        pending = folder / (name + ".pending")
        with os.fdopen(os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump(data, f, sort_keys=True)
        os.link(pending, folder / name)  # Atomic, exclusive publication to parent.
        pending.unlink()

    def selected(name, session_id):
        path = folder / name
        return path.is_file() and path.read_text() == session_id

    assert Path(main.__file__).resolve() == Path(__file__).resolve().parents[2] / "app/main.py"
    app = main.create_app(os.environ["SVI_CONFIG"])
    stage = os.environ.get("SVI_CAP22_GATE_STAGE", "asr_finish")
    assert stage in {"asr_finish", "llm_refine"}
    seen, stages = {}, []
    actual_feed, actual_finish = MockAsrSession.feed, MockAsrSession.finish
    actual_generate = MockLlmProvider.generate

    async def observed_feed(stream, pcm, start):
        value = await actual_feed(stream, pcm, start)
        runtime = app.state.sessions.runtimes.get(stream.options.session_id)
        assert runtime is not None
        seen[stream.options.session_id] = (runtime, stream)
        return value

    async def gated_finish(stream, total_samples, deadline):
        sid = stream.options.session_id
        if selected("fail-session", sid):
            raise ProviderError("unavailable", "Declared synthetic ASR failure", retryable=True)
        if stage == "asr_finish" and selected("gate-session", sid):
            assert total_samples == stream.samples == 4000
            loop = asyncio.get_running_loop()
            loop_at_entry = loop.time()
            save("gate-entered.json", {"session_id": sid, "deadline": deadline, "at": time.monotonic(),
                "loop_at_entry": loop_at_entry, "remaining_seconds": deadline - loop_at_entry})
            try:
                while not (folder / "release").exists():
                    await asyncio.sleep(.005)
                loop_at_release = loop.time()
                if loop_at_release >= deadline:
                    save("gate-control-failure.json", {"code": "CONTROL_GATE_MISSED_ORIGINAL_ASR_DEADLINE",
                        "phase": "release", "deadline": deadline, "loop_at": loop_at_release})
                assert loop_at_release < deadline, "test gate missed original ASR deadline"
                save("gate-released.json", {"at": time.monotonic(), "deadline": deadline,
                    "loop_at_release": loop_at_release, "remaining_seconds": deadline - loop_at_release})
            except asyncio.CancelledError:
                loop_at_cancel = loop.time()
                save("gate-cancelled.json", {"at": time.monotonic(), "loop_at_cancel": loop_at_cancel,
                    "deadline": deadline, "release_seen": (folder / "release").exists()})
                if loop_at_cancel >= deadline:
                    save("gate-control-failure.json", {"code": "CONTROL_GATE_MISSED_ORIGINAL_ASR_DEADLINE",
                        "phase": "cancel_at_or_after_deadline", "deadline": deadline, "loop_at": loop_at_cancel})
                raise
        return await actual_finish(stream, total_samples, deadline)

    async def gated_generate(provider, request, deadline):
        scope = current_scope.get()
        if (stage == "llm_refine" and request.purpose == "refine" and scope is not None
                and selected("gate-session", scope.session_id)):
            runtime = app.state.sessions.runtimes[scope.session_id]
            assert scope.user_id == runtime.user_id and scope.attempt_no == runtime.attempt_no == 1
            assert request.call_id and runtime.state == "refining" and runtime.ended
            raw = request.input["transcript"]
            assert isinstance(raw, str) and raw and raw == runtime.raw_text
            loop = asyncio.get_running_loop()
            entered = loop.time()
            assert 0 < deadline - entered <= 10
            assert runtime.deadline == runtime.end_accepted_at + 30 and deadline <= runtime.deadline
            save("gate-entered.json", {"stage": stage, "session_id": scope.session_id,
                "user_id": scope.user_id, "attempt_no": scope.attempt_no, "call_id": request.call_id,
                "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(), "deadline": deadline,
                "at": time.monotonic(), "loop_at_entry": entered, "remaining_seconds": deadline-entered,
                "total_deadline_loop": runtime.deadline, "end_accepted_at_loop": runtime.end_accepted_at})
            raw = None  # No text in the event files.
            try:
                while not (folder / "release").exists():
                    await asyncio.sleep(.005)
                released = loop.time()
                if released >= deadline:
                    save("gate-control-failure.json", {"code": "CONTROL_GATE_MISSED_ORIGINAL_LLM_DEADLINE",
                        "phase": "release", "deadline": deadline, "loop_at": released})
                assert released < deadline, "test gate missed original LLM deadline"
                save("gate-released.json", {"at": time.monotonic(), "deadline": deadline,
                    "loop_at_release": released, "remaining_seconds": deadline-released})
            except asyncio.CancelledError:
                cancelled = loop.time()
                save("gate-cancelled.json", {"at": time.monotonic(), "loop_at_cancel": cancelled,
                    "deadline": deadline, "release_seen": (folder / "release").exists()})
                if cancelled >= deadline:
                    save("gate-control-failure.json", {"code": "CONTROL_GATE_MISSED_ORIGINAL_LLM_DEADLINE",
                        "phase": "cancel_at_or_after_deadline", "deadline": deadline, "loop_at": cancelled})
                raise
        return await actual_generate(provider, request, deadline)

    MockAsrSession.feed, MockAsrSession.finish = observed_feed, gated_finish
    MockLlmProvider.generate = gated_generate
    actual_close = main._close_instance_resources
    pool = {}

    async def observed_close(application, lock_conn, lock_task):
        save("shutdown-entered.json", {"at": time.monotonic(), "fenced": application.state.instance_fenced})
        close_db = application.state.db.close

        async def observed_db_close():
            pool["before_dispose"] = application.state.db.engine.pool.checkedout()
            return await close_db()

        application.state.db.close = observed_db_close
        return await actual_close(application, lock_conn, lock_task)

    main._close_instance_resources = observed_close
    config = app.state.config
    assert config.asr.provider == config.llm.provider == "mock"
    assert config.asr.finish_timeout_ms == config.llm.call_timeout_ms == 10000
    assert config.pipeline.stop_to_result_budget_ms == 30000
    assert config.asr.decode_timeout_ms == 3000 and config.retry.audio_ttl_seconds == 600
    save("child-binding.json", {"pid": os.getpid(), "parent_pid": os.getppid(),
        "app_path": str(Path(main.__file__).resolve()), "uvicorn_version": uvicorn.__version__,
        "uvicorn_server_sha256": hashlib.sha256(Path(uvicorn.server.__file__).read_bytes()).hexdigest(),
        "providers": [config.asr.provider, config.llm.provider], "gate_stage": stage,
        "budgets_ms": {"decode": config.asr.decode_timeout_ms, "finish": config.asr.finish_timeout_ms,
            "llm": config.llm.call_timeout_ms, "stop_to_result": config.pipeline.stop_to_result_budget_ms}})
    sock = socket.socket(fileno=fd)
    assert sock.getsockname()[0] == "127.0.0.1"
    # Uvicorn captures SIGTERM itself. Its documented final signal replay goes
    # to this fallback, as in the existing owned launcher; no early begin_close.
    prior = signal.signal(signal.SIGTERM, lambda *_: None)
    server = main.create_server(app, loop="uvloop", timeout_graceful_shutdown=35)
    handle_exit = server.handle_exit

    def observed_signal(sig, frame):
        handle_exit(sig, frame)
        if not stages:
            stages.append(sig)
            save("signal-observed.json", {"signal": sig, "pid": os.getpid(), "at": time.monotonic()})

    server.handle_exit = observed_signal
    returned = False
    try:
        server.run(sockets=[sock])
        returned = True
    finally:
        sock.close()
        signal.signal(signal.SIGTERM, prior)
        manager = getattr(app.state, "sessions", None)
        save("child-final.json", {"server_returned": returned,
            "fenced": bool(getattr(app.state, "instance_fenced", False)),
            "stopping": bool(getattr(app.state, "stopping", False)), "pool": pool,
            "runtime_refs": [{"session_id": sid, "pcm_bytes": len(r.audio),
                "asr_attached": r.asr is not None, "mock_cancelled": s.cancelled,
                "processing_done": r.task is None or r.task.done()} for sid, (r, s) in seen.items()],
            "runtime_count": len(manager.runtimes) if manager else None,
            "background_count": len(manager.background_tasks) if manager else None,
            "processing_count": len(manager._processing_tasks) if manager else None})


if __name__ == "__main__":
    run(Path(sys.argv[1]), int(sys.argv[2]))
