"""Isolated recovery child; fault gates never ship in application code."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from sqlalchemy import event

from app import main


def run(folder: Path, mode: str):
    assert mode in {"terminate", "unlock", "timeout", "cleanup_hang", "loop_hang"}
    logging.basicConfig(level=logging.INFO)
    actual_database = main.Database

    def database(config):
        instance = actual_database(config)

        @event.listens_for(instance.engine.sync_engine, "before_cursor_execute")
        def inject(connection, cursor, statement, parameters, context, executemany):
            if ("case when pg_backend_pid()" not in statement or not (folder / "inject").exists()
                    or (folder / "injected").exists() or mode == "terminate"):
                return
            (folder / "injected").write_text(str(os.getpid()))
            if mode == "timeout":
                cursor.execute("SELECT pg_sleep(3)")  # Real PG wait; original 2s production timeout cancels it.
            elif mode == "loop_hang":
                time.sleep(120)  # Declared event-loop block; Linux watchdog must kill this process.
            else:
                cursor.execute("SELECT pg_advisory_unlock(%s)" % config.server.instance_lock_key)

        return instance

    main.Database = database
    if mode == "cleanup_hang":
        async def stuck_cleanup(*args):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            (folder / "cleanup-child-pid").write_text(str(child.pid))
            await asyncio.Event().wait()
        main._close_instance_resources = stuck_cleanup
    try:
        main.main()
    finally:
        state = main.app.state
        (folder / "cleanup.json").write_text(json.dumps({
            "pid": os.getpid(), "fenced": getattr(state, "instance_fenced", None),
            "checkedout": state.db.engine.pool.checkedout() if getattr(state, "db", None) else None,
            "owned_tasks_done": all(getattr(state, name, None) is None or getattr(state, name).done()
                for name in ("startup_task", "probe_task", "cleanup_task", "llm_prepare_task")),
        }))


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2])
