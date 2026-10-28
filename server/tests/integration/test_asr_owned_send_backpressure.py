"""Two short real pipe/OS controls; no DB, model, audio quality or TTL600 claim."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid
import weakref

import pytest

from app.services import asr_core
from tests.integration.asr_owned_receive_gate_fixture import USER_BYTES, bootstrap
from tests.integration.test_asr_real_stop_second_cancel import alive


def private_json(path, body):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(body, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("release,seconds", [(False, 3), (True, 10)],
                         ids=["blocked-original-3s-timeout", "release-inside-original-10s"])
async def test_owned_sender_backpressure_and_known_pickle_lifetime(
    monkeypatch, tmp_path, record_property, release, seconds,
):
    if os.name != "posix":
        pytest.skip("This owned socketpair/TERM/kill control is POSIX-only")
    folder = tmp_path / ("owned-receive-" + uuid.uuid4().hex)
    folder.mkdir(mode=0o700)
    pcm = bytearray(b"\x49\x00" * (USER_BYTES // 2))
    owner = {"parent_pid": os.getpid(), "nonce": uuid.uuid4().hex,
             "user_bytes": USER_BYTES, "pcm_sha256": hashlib.sha256(pcm).hexdigest()}
    private_json(folder / "owner.json", owner)
    monkeypatch.setenv("SVI_OWNED_RECEIVE_GATE_DIR", str(folder))
    monkeypatch.setattr(asr_core, "_worker_main", bootstrap)
    worker, loop = asr_core.DecodeWorker(), asyncio.get_running_loop()
    trace = {"completed": False, "release": release, "seconds": seconds,
             "scope": {"real_spawn_pipe_decode_stop": True, "synthetic_child": True,
                       "receive_gate_injection": True, "real_model": False, "ttl600": False,
                       "all_pickle_copies_observed": False, "db_api_desktop": False},
             "sender": {}, "buffers": [], "cleanup": {}, "issues": []}
    refs, sending, sent = [], threading.Event(), threading.Event()
    task = process = popen = connection = None

    def clock():
        return {"monotonic": time.monotonic(), "loop": loop.time()}

    def events():
        rows = []
        for path in folder.glob("child-*.jsonl"):
            for line in path.read_text().splitlines(keepends=True):
                if not line.endswith("\n"):
                    continue
                row = json.loads(line)
                assert row["parent_pid"] == owner["parent_pid"] and row["nonce"] == owner["nonce"]
                assert path.name == f"child-{row['pid']}.jsonl"
                rows.append(row)
        return rows

    async def until(predicate, budget):
        async with asyncio.timeout(budget):
            while not predicate():
                await asyncio.sleep(.005)

    def sample():
        # Inspect only our recorded sender, connection, functions and buf lengths.
        # Do not retain frames, f_locals, payload, traceback or unrelated threads.
        rows = []
        frame = sys._current_frames().get(trace["sender"].get("thread_id", -1))
        try:
            while frame is not None:
                function = frame.f_code.co_name
                filename = frame.f_code.co_filename
                if (filename.endswith("/multiprocessing/connection.py")
                        and function in {"send", "_send_bytes", "_send"}):
                    # Materialize locals only for this connection's send frames;
                    # executor internals must keep their own reference lifecycle.
                    local = frame.f_locals
                    if local.get("self") is connection:
                        value = local.get("buf")
                        rows.append({"function": function, "line": frame.f_lineno,
                                     "buf_type": type(value).__name__,
                                     "buf_length": len(value) if isinstance(value, (bytes, bytearray, memoryview)) else None})
                        del value
                    del local
                elif filename == __file__ and function in {"observed_sender", "observed_bytes"}:
                    rows.append({"function": function, "line": frame.f_lineno})
                frame = frame.f_back
        finally:
            del frame
        return {"clock": clock(), "sender_finally_seen": sent.is_set(), "frames": rows,
                "pickle_refs_alive": [reference() is not None for reference in refs],
                "original_pcm_length": len(pcm)}

    async def cleanup():
        errors = []
        if task is not None and not task.done():
            task.cancel()
        try:
            async with asyncio.timeout(5):
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
                await worker.stop()
        except BaseException as exc:
            errors.append({"stage": "owned_stop", "type": type(exc).__name__})
        emergency = False
        if process is not None and alive(process, popen):
            assert process._parent_pid == os.getpid() and process.pid == trace["pid"]
            emergency = True
            process.kill()
            await asyncio.to_thread(process.join, 2)
        trace["cleanup"] = {"errors": errors, "emergency": emergency,
            "old_exited": process is None or not alive(process, popen),
            "exitcode": popen.returncode if popen is not None else None,
            "worker_handles_cleared": worker.process is None and worker.connection is None,
            "after": sample()}

    try:
        # Normal production prewarm input; gate is only before the next recv.
        warm = await worker.decode((bytes(32000), str(folder), "auto", ""), loop.time() + 120)
        assert warm == {"words": [], "segments": [], "seconds": 0.0}
        process, connection = worker.process, worker.connection
        popen = process._popen
        assert process._parent_pid == os.getpid()
        trace["pid"] = process.pid
        await until(lambda: any(row["kind"] == "before_first_user_recv" for row in events()), 2)
        original_sender, original_bytes = worker._send_before_deadline, connection._send_bytes

        def observed_bytes(buf):
            item = {"type": type(buf).__name__, "length": len(buf),
                    "readonly": buf.readonly if isinstance(buf, memoryview) else None}
            try:
                refs.append(weakref.ref(buf))
                item["weakref_supported"] = True
            except TypeError:
                item["weakref_supported"] = False
            trace["buffers"].append(item)
            return original_bytes(buf)

        def observed_sender(conn, args, deadline):
            assert conn is connection
            trace["sender"].update(thread_id=threading.get_ident(), start_monotonic=time.monotonic(),
                                     mapped_monotonic_deadline=deadline)
            sending.set()
            try:
                return original_sender(conn, args, deadline)
            except BaseException as exc:
                trace["sender"]["exception_type"] = type(exc).__name__
                raise
            finally:
                trace["sender"]["finally_monotonic"] = time.monotonic()
                sent.set()  # Also inspect absence of owned frames; flag alone is not proof.

        monkeypatch.setattr(connection, "_send_bytes", observed_bytes)
        monkeypatch.setattr(worker, "_send_before_deadline", observed_sender)
        started = loop.time()
        deadline = started + seconds
        trace["started_loop"], trace["deadline_loop"] = started, deadline
        task = asyncio.create_task(worker.decode((pcm, str(folder), "auto", ""), deadline))
        await until(lambda: sending.is_set() and any(row["function"] == "_send" for row in sample()["frames"]), 1)
        trace["blocked_first"] = sample()
        await asyncio.sleep(.05)  # Observe actual pending write; not an injected serializer wait.
        trace["blocked_second"] = sample()
        assert not task.done() and not sent.is_set()
        assert any(row["function"] == "_send" for row in trace["blocked_second"]["frames"])
        assert any(item["length"] > USER_BYTES and item["weakref_supported"] for item in trace["buffers"]), "CONTROL: known pickle view not observable"
        assert not any(row["kind"] == "actual_recv_return" and row["ordinal"] == 2 for row in events())
        if release:
            assert loop.time() < deadline
            temporary = folder / "release.complete.tmp"
            private_json(temporary, {"nonce": owner["nonce"]})
            try:
                os.link(temporary, folder / "release.json")
            finally:
                temporary.unlink()
            trace["released"] = clock()
        try:
            async with asyncio.timeout_at(deadline + 3):  # Observer rescue, not changed decode deadline.
                answer = await asyncio.shield(task)
            trace["outcome"] = "success"
            assert release and answer == {"words": [(0.0, .25, "本机背压测试。")], "segments": [.25], "seconds": 0.0}
        except TimeoutError:
            assert task.done(), "CONTROL: outer rescue elapsed before production decode ended"
            assert not release and isinstance(task.exception(), TimeoutError)
            trace["outcome"] = "timeout"
        trace["decode_return"] = sample()
        trace["old_alive_at_return"] = alive(process, popen)
        trace["exitcode_at_return"] = popen.returncode
        # No forced GC or retained payload reference. Observe separately from thread return.
        end_observation = loop.time() + 1
        while any(reference() is not None for reference in refs) and loop.time() < end_observation:
            await asyncio.sleep(.005)
        trace["pickle_observation_end"] = sample()
        trace["children"] = events()
        if not trace["decode_return"]["sender_finally_seen"] or trace["decode_return"]["frames"]:
            trace["issues"].append("sender invocation still owned frames at decode return")
        if any(trace["pickle_observation_end"]["pickle_refs_alive"]):
            trace["issues"].append("observed pickle memoryview still referenced after observation")
        assert not pcm
        if release:
            assert trace["decode_return"]["clock"]["loop"] < deadline and trace["old_alive_at_return"]
            assert any(row["kind"] == "synthetic_user_decode" for row in trace["children"])
        else:
            assert deadline <= trace["decode_return"]["clock"]["loop"] <= deadline + 2
            assert not trace["old_alive_at_return"] and trace["exitcode_at_return"] == -signal.SIGKILL
            assert any(row["kind"] == "term_ignored" for row in trace["children"])
            assert not any(row["kind"] == "synthetic_user_decode" for row in trace["children"])
        assert not any(row["kind"] == "receive_gate_safety_exit" for row in trace["children"])
        trace["completed"] = True
    finally:
        try:
            await cleanup()
        finally:
            path = folder / "sender-report.json"
            private_json(path, trace)
            record_property("owned_sender_report", json.dumps({"path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "completed": trace["completed"],
                "issues": trace["issues"], "scope": trace["scope"]}, sort_keys=True))
    assert trace["completed"] and not trace["issues"], trace["issues"]
    assert not trace["cleanup"]["errors"] and not trace["cleanup"]["emergency"]
    assert trace["cleanup"]["old_exited"] and trace["cleanup"]["worker_handles_cleared"]
