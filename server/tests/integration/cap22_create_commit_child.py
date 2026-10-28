"""G2 child: reuse G1 owned server; gate one create COMMIT, never lifecycle."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import time

from app.models import VoiceSession
from app.providers.asr.mock import MockAsrProvider
from app.services.session_runtime import SessionManager
from tests.integration import cap22_sigterm_child as g1


def run(folder, fd):
    def save(name, value):
        temporary = folder / (name + ".pending")
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f:
            json.dump(value, f, sort_keys=True)
        os.link(temporary, folder / name)
        temporary.unlink()

    actual_admission = SessionManager._admission_session
    actual_register, actual_open = SessionManager._register_runtime, MockAsrProvider.open
    used, committed, seen, opened = False, False, {}, []

    @asynccontextmanager
    async def admission(manager):
        nonlocal used, committed
        async with actual_admission(manager) as db:
            target_path = folder / "create-target.json"
            if used or not target_path.is_file():
                yield db
                return
            target = json.loads(target_path.read_text())
            actual_commit = db.commit

            async def commit():
                nonlocal used, committed
                candidates = [row for row in [*db.identity_map.values(), *db.new]
                    if isinstance(row, VoiceSession) and row.user_id == target["user_id"]
                    and row.client_request_id == target["request_key"]]
                if not candidates or used:
                    return await actual_commit()
                assert len(candidates) == 1
                session = candidates[0]
                used = True
                assert target["stage"] in {"before_commit", "after_commit_before_publish"}
                if target["stage"] == "after_commit_before_publish":
                    await actual_commit()  # Actual PostgreSQL COMMIT, not a forged response.
                    committed = True
                assert session.id not in manager.runtimes and manager._operations
                save("create-gate-entered.json", {"stage": target["stage"],
                    "session_id": session.id, "user_id": session.user_id,
                    "request_key": session.client_request_id, "committed": committed,
                    "in_transaction": db.in_transaction(), "runtime_published": False,
                    "operation_count": len(manager._operations), "at": time.monotonic()})
                try:
                    async with asyncio.timeout(8):  # Control guard only, no product deadline changes.
                        while not (folder / "create-release").is_file():
                            await asyncio.sleep(.005)
                except BaseException as error:
                    save("create-control-failure.json", {"type": type(error).__name__,
                        "committed": committed, "stage": target["stage"]})
                    raise
                save("create-gate-release.json", {"closing": manager._closing,
                    "operation_count": len(manager._operations), "at": time.monotonic()})
                if not committed:
                    await actual_commit()
                    committed = True
                save("create-commit-returned.json", {"committed": True,
                    "in_transaction": db.in_transaction(), "at": time.monotonic()})

            db.commit = commit
            try:
                yield db
            finally:
                db.commit = actual_commit

    def registered(manager, runtime):
        answer = actual_register(manager, runtime)
        seen[runtime.session_id] = runtime
        return answer

    async def opened_provider(provider, options):
        opened.append({"session_id": options.session_id, "attempt_no": options.attempt_no})
        return await actual_open(provider, options)

    SessionManager._admission_session, SessionManager._register_runtime = admission, registered
    MockAsrProvider.open = opened_provider
    try:
        g1.run(folder, fd)  # Actual main.create_server; no early begin_close/ready mutation.
    finally:
        save("create-observer-final.json", {"gate_used": used, "commit_completed": committed,
            "provider_opens": opened,
            "runtime_refs": [{"session_id": sid, "state": r.state, "error_code": r.error_code,
                "pcm_bytes": len(r.audio), "asr_attached": r.asr is not None,
                "processing_done": r.task is None or r.task.done()} for sid, r in seen.items()]})


if __name__ == "__main__":
    run(Path(sys.argv[1]), int(sys.argv[2]))
