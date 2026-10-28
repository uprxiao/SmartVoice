"""Actual SessionManager close/background scheduling, no DB/provider doubles.

Old busy-spin blocks this event loop: root must supervise the pytest process
externally. An asyncio timeout alone cannot interrupt a synchronous spin.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.services.session_runtime import SessionManager


@pytest.mark.parametrize("outcome", ["success", "failure", "spawn_cleanup"])
def test_close_drains_completed_background_callbacks_and_late_cleanup(outcome):
    async def run():
        manager = SessionManager(SimpleNamespace(), None, None, None, None)
        entered, release = asyncio.Event(), asyncio.Event()
        child_started, child_finished = asyncio.Event(), asyncio.Event()
        observed = []
        derived = []

        async def child():
            child_started.set()
            observed.append("child_started")
            await asyncio.sleep(0)  # child itself has asynchronous cleanup work
            observed.append("child_finished")
            child_finished.set()

        def spawn_child(_completed):
            before = set(manager.background_tasks)
            manager._background(child())
            derived.extend(manager.background_tasks - before)

        async def cleanup():
            entered.set()
            await release.wait()
            observed.append("parent_released")
            if outcome == "spawn_cleanup":
                # Completion-triggered cleanup is added through the real API;
                # neither the manager callback nor its collection is replaced.
                asyncio.current_task().add_done_callback(spawn_child)
            if outcome == "failure":
                raise ValueError("synthetic cleanup failure")

        manager._background(cleanup())
        assert len(manager.background_tasks) == 1
        parent = next(iter(manager.background_tasks))
        await entered.wait()
        assert not parent.done() and not release.is_set()
        release.set()
        # Deliberately NO await/sleep between releasing cleanup and close().
        # Parent runs before the new close owner; its done callbacks run later.
        if outcome == "failure":
            with pytest.raises(RuntimeError, match="^SESSION_SHUTDOWN_PERSISTENCE_INCOMPLETE$"):
                await manager.close()
        else:
            await manager.close()
        assert parent.done() and not parent.cancelled()
        assert not manager.background_tasks
        assert manager._close_task.done() and not manager._close_task.cancelled()
        assert manager._auth_reads._closed and manager._auth_reads._runner is None
        assert manager._background_failed is (outcome == "failure")
        if outcome == "failure":
            assert isinstance(parent.exception(), ValueError)
            assert observed == ["parent_released"]
        elif outcome == "spawn_cleanup":
            assert parent.result() is None
            assert child_started.is_set() and child_finished.is_set()
            assert len(derived) == 1 and derived[0].done() and derived[0].result() is None
            assert observed == ["parent_released", "child_started", "child_finished"]
        else:
            assert parent.result() is None and observed == ["parent_released"]
    asyncio.run(run())
