"""Synthetic history preparation for the owned actual desktop UI fixture.

The review flag is explicit test-data preparation. It does not represent real
microphone/model quality and never writes an insertion claim or receipt.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import locale
from pathlib import Path
import struct
import sys
import uuid

from desktop_live_environment import environment, write_new

RAW = "嗯那个，帮我加一个按钮，不要修改数据库。"
REFINED = "帮我加一个按钮，不要修改数据库。"
EMAIL = "desktop-live@example.test"


async def snapshot(path, session_id=None):
    import asyncpg
    value = environment(path)
    conn = await asyncpg.connect(value["database_url"].replace("postgresql+asyncpg:", "postgresql:"))
    try:
        rows = await conn.fetch("""SELECT s.id AS session_id,s.state,s.learning_enabled,
            s.received_samples,u.email,r.id AS result_id,r.attempt_no,
            a.kind AS attempt_kind,r.raw_text,r.refined_text,r.review_required,r.review_reason,
            r.text_hash FROM sessions s JOIN users u ON u.id=s.user_id
            JOIN results r ON r.session_id=s.id AND r.user_id=s.user_id
            JOIN session_attempts a ON a.user_id=r.user_id AND a.session_id=r.session_id
            AND a.attempt_no=r.attempt_no
            WHERE ($1::text IS NULL OR s.id=$1::text) ORDER BY r.attempt_no""", session_id)
        receipts = await conn.fetch("""SELECT i.id,i.session_id,i.result_id,i.status,
            i.observable,i.error_code FROM insertions i JOIN sessions s
            ON s.id=i.session_id AND s.user_id=i.user_id
            WHERE ($1::text IS NULL OR s.id=$1::text) ORDER BY i.id""", session_id)
        learning = await conn.fetchval("SELECT count(*) FROM learning_events")
        users = await conn.fetch("SELECT email,learning_enabled FROM users ORDER BY email")
        return {"results": [dict(row) for row in rows], "insertions": [dict(row) for row in receipts],
                "learning_events": learning, "users": [dict(row) for row in users]}
    finally:
        await conn.close()


async def seed(path, slot="history"):
    import asyncpg
    import httpx
    import websockets
    value = environment(path)
    output = Path(value["run_dir"]) / f"{slot}-seed.json"
    before = await snapshot(path)
    if output.exists() or (slot == "history" and before["results"]):
        raise RuntimeError("Fresh empty owned history required")
    if slot == "journal" and (len(before["results"]) != 2 or len(before["insertions"]) != 1
            or before["insertions"][0]["status"] != "inserted" or before["learning_events"] != 0):
        raise RuntimeError("Completed owned baseline required before the independent crash scenario")
    private = json.loads(Path(value["private_file"]).read_text(encoding="utf-8"))
    async with httpx.AsyncClient(base_url=value["origin"], timeout=15, trust_env=False) as client:
        login = await client.post("/api/auth/login", json={"email": EMAIL, "password": private["password"],
            "client_type": "desktop", "device": {"installation_id": str(uuid.uuid4()),
            "name": "Synthetic history preparation", "platform": "windows", "client_version": "0.0.2"}})
        if login.status_code != 200:
            raise RuntimeError(f"Owned fixture login failed ({login.status_code})")
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        try:
            request_id = str(uuid.uuid4())
            created = await client.post("/api/v1/sessions", headers={**headers, "Idempotency-Key": request_id},
                json={"client_request_id": request_id, "learning_enabled": False})
            if created.status_code != 201:
                raise RuntimeError(f"Synthetic session creation failed ({created.status_code})")
            sid = created.json()["session_id"]
            async with websockets.connect(value["origin"].replace("http:", "ws:") + f"/ws/v1/sessions/{sid}/audio",
                    additional_headers=headers, proxy=None) as ws:
                async with asyncio.timeout(15):
                    if json.loads(await ws.recv())["type"] != "ready":
                        raise RuntimeError("Synthetic audio stream not ready")
                    for seq in range(4):
                        await ws.send(struct.pack("<II", seq, 4000) + b"\x00\x00" * 4000)
                    for _ in range(4):
                        if json.loads(await ws.recv())["type"] != "audio.ack":
                            raise RuntimeError("Synthetic audio acknowledgement differs")
                    await ws.send(json.dumps({"type": "audio.end", "last_seq": 3, "total_samples": 16000}))
                    while True:
                        event = json.loads(await ws.recv())
                        if event["type"] == "result.ready":
                            result = event["result"]
                            break
                        if event["type"] in {"error", "cancelled", "session.empty"}:
                            raise RuntimeError("Synthetic result failed: " + event["type"])
            if result["raw_text"] != RAW or result["refined_text"] != REFINED or result["review_required"]:
                raise RuntimeError("Frozen synthetic provider result differs")
            conn = await asyncpg.connect(value["database_url"].replace("postgresql+asyncpg:", "postgresql:"))
            try:
                # Only this exact owned result; preserve its text/hash/attempt.
                changed = await conn.fetch("""UPDATE results r SET review_required=true,
                    review_reason='llm_unavailable' FROM users u WHERE r.user_id=u.id
                    AND u.email=$1 AND r.id=$2 AND r.session_id=$3 AND NOT r.review_required
                    RETURNING r.id""", EMAIL, result["result_id"], sid)
                if len(changed) != 1:
                    raise RuntimeError("Owned history review fixture did not update exactly one result")
            finally:
                await conn.close()
        finally:
            logout = await client.post("/api/auth/logout", headers=headers)
            if logout.status_code != 204:
                raise RuntimeError(f"Preparation login revocation failed ({logout.status_code})")
    facts = await snapshot(path, sid)
    if len(facts["results"]) != 1 or facts["insertions"] or facts["learning_events"] != 0:
        raise RuntimeError("Synthetic history baseline differs")
    row = facts["results"][0]
    if row["state"] != "ready" or row["learning_enabled"] or row["received_samples"] != 16000:
        raise RuntimeError("Synthetic audio/session metadata differs")
    record = {"scope": "synthetic zero PCM + mock providers; explicit review flag fixture, no microphone/quality/S5 pass",
        "cli_stdout_encoding": sys.stdout.encoding, "cli_json_ascii_escaped": True,
        "session_id": sid, "result_id": result["result_id"], "review_flag_prepared_by_fixture": True,
        "raw_text": RAW, "refined_text": REFINED, "expected_native_text": "BEGIN " + REFINED + " END",
        "expected_caret_utf16": 22, "expected_selection_utf16_len": 0,
        "baseline": json.loads(json.dumps(facts, default=str))}
    write_new(output, record)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("seed", "snapshot"))
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--slot", choices=("history", "journal"), default="history")
    args = parser.parse_args()
    phase = "read_journal_scope"
    try:
        scope_id = None
        if args.command == "snapshot" and args.slot == "journal":
            scope_id = json.loads((Path(environment(args.environment)["run_dir"]) / "journal-seed.json").read_text(encoding="utf-8"))["session_id"]
        phase = "owned_database_command"
        result = asyncio.run(seed(args.environment, args.slot) if args.command == "seed" else snapshot(args.environment, scope_id))
    except Exception as error:
        if args.command == "snapshot":
            failure = Path(environment(args.environment)["run_dir"]) / f"{args.slot}-snapshot-failure.json"
            if not failure.exists():
                # No database URL, exception message, account or token in artifacts.
                write_new(failure, {"status": "fail", "phase": phase, "error_type": type(error).__name__,
                    "default_text_encoding": locale.getpreferredencoding(False), "utf8_mode": sys.flags.utf8_mode})
        raise
    print(json.dumps(result, ensure_ascii=True, default=str))  # Portable JSON pipe; decoded fixture text stays exact.
