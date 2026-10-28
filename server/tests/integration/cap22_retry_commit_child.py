"""G2 retry child: real admission COMMIT gate, official G1 SIGTERM owner.

The historical stage name after_commit_before_publish means before processing
acceptance/handoff here: retry already publishes its PCM owner before COMMIT.
No transaction result, runtime state, deadline or shutdown method is forged.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from app.models import VoiceSession
from app.providers.asr.mock import MockAsrProvider, MockAsrSession
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
    actual_feed, actual_finish = MockAsrSession.feed, MockAsrSession.finish
    used, committed, owned_manager = False, False, None
    seen, opened, fed, finished, known_pcm, leases = {}, [], [], [], [], []

    @asynccontextmanager
    async def admission(manager):
        nonlocal used, committed
        async with actual_admission(manager) as db:
            target_path = folder / "retry-target.json"
            if used or not target_path.is_file():
                yield db
                return
            target = json.loads(target_path.read_text())
            previous = manager.runtimes[target["session_id"]]
            audio, lease = previous.audio, previous.audio_lease
            assert previous.user_id == target["user_id"] and previous.attempt_no == 1
            assert previous.state == "failed" and previous.ended and len(audio) == 8000
            assert lease is not None and lease.owner is previous and lease.status == "available"
            expiry, lease_deadline, timer = lease.expires_at, lease.deadline, lease.timer
            assert expiry == target["expiry_epoch"] and time.time() < expiry
            known_pcm.append(audio)  # Same mutable object, no extra PCM copy.
            leases.append(lease)
            actual_commit = db.commit

            async def commit():
                nonlocal used, committed
                candidates = [row for row in [*db.identity_map.values(), *db.new]
                    if isinstance(row, VoiceSession) and row.user_id == target["user_id"]
                    and row.id == target["session_id"] and row.current_attempt_no == 2]
                if not candidates or used:
                    return await actual_commit()
                assert len(candidates) == 1
                used = True
                runtime = manager.runtimes[target["session_id"]]
                assert runtime is not previous and runtime.attempt_no == 2 and runtime.task is None
                assert runtime.audio is audio and not previous.audio and previous.audio_lease is None
                assert runtime.audio_lease is lease and lease.owner is runtime and lease.timer is timer
                assert lease.expires_at == expiry and lease.deadline == lease_deadline
                assert runtime.deadline == runtime.end_accepted_at + 30
                assert target["stage"] in {"before_commit", "after_commit_before_publish"}
                if target["stage"] == "after_commit_before_publish":
                    await actual_commit()  # Real PostgreSQL COMMIT before the return gate.
                    committed = True
                entered = asyncio.get_running_loop().time()
                save("retry-gate-entered.json", {"stage": target["stage"],
                    "session_id": runtime.session_id, "user_id": runtime.user_id,
                    "attempt_no": runtime.attempt_no, "request_key": target["request_key"],
                    "committed": committed, "in_transaction": db.in_transaction(),
                    "runtime_published": manager.runtimes[runtime.session_id] is runtime,
                    "processing_accepted": runtime.processing_accepted, "task_present": runtime.task is not None,
                    "operation_count": len(manager._operations), "pcm_bytes": len(audio),
                    "pcm_sha256": hashlib.sha256(audio).hexdigest(), "previous_pcm_bytes": len(previous.audio),
                    "same_pcm_and_lease": True, "expiry_epoch": expiry, "lease_deadline_loop": lease_deadline,
                    "end_accepted_at_loop": runtime.end_accepted_at, "total_deadline_loop": runtime.deadline,
                    "asr_deadline_loop": manager._asr_deadline(runtime), "entered_loop": entered,
                    "at": time.monotonic()})
                try:
                    async with asyncio.timeout(8):  # Gate supervision only; no product budget change.
                        while not (folder / "retry-release").is_file():
                            await asyncio.sleep(.005)
                    released = asyncio.get_running_loop().time()
                    if released >= min(runtime.end_accepted_at + 10, lease_deadline):
                        raise AssertionError("CONTROL_GATE_MISSED_ORIGINAL_RETRY_DEADLINE")
                except BaseException as error:
                    save("retry-control-failure.json", {"type": type(error).__name__,
                        "committed": committed, "stage": target["stage"]})
                    raise
                save("retry-gate-release.json", {"closing": manager._closing,
                    "operation_count": len(manager._operations), "pcm_bytes": len(audio),
                    "state": runtime.state, "error_code": runtime.error_code,
                    "lease_status": lease.status, "original_expiry_unchanged": lease.expires_at == expiry,
                    "original_total_deadline_unchanged": runtime.deadline == runtime.end_accepted_at + 30,
                    "released_loop": released, "at": time.monotonic()})
                if not committed:
                    await actual_commit()
                    committed = True
                save("retry-commit-returned.json", {"committed": True,
                    "in_transaction": db.in_transaction(), "at": time.monotonic()})

            db.commit = commit
            try:
                yield db
            finally:
                db.commit = actual_commit

    def registered(manager, runtime):
        nonlocal owned_manager
        owned_manager = manager
        answer = actual_register(manager, runtime)
        seen[(runtime.session_id, runtime.attempt_no)] = runtime
        return answer

    async def opened_provider(provider, options):
        opened.append({"session_id": options.session_id, "attempt_no": options.attempt_no})
        return await actual_open(provider, options)

    async def observed_feed(stream, pcm, start):
        answer = await actual_feed(stream, pcm, start)
        fed.append({"session_id": stream.options.session_id, "attempt_no": stream.options.attempt_no,
            "start": start, "samples": len(pcm) // 2, "sha256": hashlib.sha256(pcm).hexdigest()})
        return answer

    async def observed_finish(stream, total_samples, deadline):
        result = await actual_finish(stream, total_samples, deadline)
        finished.append({"session_id": stream.options.session_id, "attempt_no": stream.options.attempt_no,
            "samples": total_samples, "deadline_loop": deadline,
            "returned_loop": asyncio.get_running_loop().time(), "nonempty_final": bool(result.text)})
        return result

    SessionManager._admission_session, SessionManager._register_runtime = admission, registered
    MockAsrProvider.open = opened_provider
    MockAsrSession.feed, MockAsrSession.finish = observed_feed, observed_finish
    try:
        g1.run(folder, fd)  # Real main.create_server, unchanged signal/lifespan handling.
    finally:
        save("retry-observer-final.json", {"gate_used": used, "commit_completed": committed,
            "provider_opens": opened, "feeds": fed, "finishes": finished,
            "operation_count": len(owned_manager._operations) if owned_manager else 0,
            "websocket_count": len(owned_manager._websockets) if owned_manager else 0,
            "closing_runtime_count": len(owned_manager._closing_runtimes) if owned_manager else 0,
            "auth_task_count": len(owned_manager._auth_tasks) if owned_manager else 0,
            "known_pcm_bytes": [len(audio) for audio in known_pcm],
            "leases": [{"expiry_epoch": lease.expires_at, "deadline_loop": lease.deadline,
                "status": lease.status, "owner_present": lease.owner is not None,
                "timer_present": lease.timer is not None} for lease in leases],
            "runtime_refs": [{"session_id": sid, "attempt_no": attempt, "state": r.state,
                "error_code": r.error_code, "pcm_bytes": len(r.audio), "asr_attached": r.asr is not None,
                "processing_done": r.task is None or r.task.done()} for (sid, attempt), r in seen.items()]})


if __name__ == "__main__":
    run(Path(sys.argv[1]), int(sys.argv[2]))
