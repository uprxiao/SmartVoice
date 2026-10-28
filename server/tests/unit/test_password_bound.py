import asyncio
import threading
from types import SimpleNamespace
import pytest
from app.api.auth import password_work


def test_repeated_cancellation_does_not_release_live_hash_slot():
    async def run():
        entered, release = threading.Event(), threading.Event()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(password_slots=asyncio.Semaphore(1),
                                      config=SimpleNamespace(auth=object()))))
        def hashing(config):
            entered.set()
            release.wait(timeout=5)
            return 'done'
        task = asyncio.create_task(password_work(request, hashing))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel(); await asyncio.sleep(.01)
            task.cancel(); await asyncio.sleep(.01)
            assert request.app.state.password_slots.locked()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not request.app.state.password_slots.locked()
    asyncio.run(run())
