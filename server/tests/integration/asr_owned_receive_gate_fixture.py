"""Owned child receive fault only; production worker loop/Pipe stay real."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import time

USER_BYTES = 2 * 1024 * 1024  # 65.536 seconds at 16k mono s16le; within 180s.


def bootstrap(connection):
    from app.services import asr_core

    folder = Path(os.environ["SVI_OWNED_RECEIVE_GATE_DIR"]).resolve()
    owner = json.loads((folder / "owner.json").read_text())
    assert folder.name.startswith("owned-receive-")
    assert os.getppid() == owner["parent_pid"] and len(owner["nonce"]) == 32
    assert owner["user_bytes"] == USER_BYTES

    def note(kind, **fields):
        row = {"kind": kind, "pid": os.getpid(), "parent_pid": os.getppid(),
               "nonce": owner["nonce"], "monotonic": time.monotonic(), **fields}
        payload = (json.dumps(row, sort_keys=True) + "\n").encode()
        fd = os.open(folder / f"child-{os.getpid()}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            assert os.write(fd, payload) == len(payload)
        finally:
            os.close(fd)

    class Receiver:
        count = 0

        def recv(self):
            if self.count == 1:
                signal.signal(signal.SIGTERM, lambda signum, frame: note("term_ignored"))
                note("before_first_user_recv")
                cutoff = time.monotonic() + 20  # Fixture rescue; cannot count as production stop.
                while not (folder / "release.json").exists():
                    if os.getppid() != owner["parent_pid"] or time.monotonic() >= cutoff:
                        note("receive_gate_safety_exit")
                        os._exit(88)
                    time.sleep(.005)
                assert json.loads((folder / "release.json").read_text()) == {"nonce": owner["nonce"]}
                note("gate_released_before_actual_recv")
            message = connection.recv()
            self.count += 1
            note("actual_recv_return", ordinal=self.count)
            return message

        def send(self, value):
            return connection.send(value)

        def close(self):
            return connection.close()

    def transcribe(pcm, model_dir, language, prompt):
        assert Path(model_dir).resolve() == folder and language == "auto" and prompt == ""
        if len(pcm) == 32000 and not any(pcm):
            note("synthetic_warm_return")
            return {"words": [], "segments": [], "seconds": 0.0}
        assert len(pcm) == USER_BYTES and hashlib.sha256(pcm).hexdigest() == owner["pcm_sha256"]
        note("synthetic_user_decode", samples=len(pcm) // 2)
        return {"words": [(0.0, .25, "本机背压测试。")], "segments": [.25], "seconds": 0.0}

    # Spawn imports the original module, unlike the parent's target monkeypatch.
    asr_core.transcribe = transcribe
    asr_core._worker_main(Receiver())
