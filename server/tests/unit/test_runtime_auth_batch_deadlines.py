"""Independent caller deadlines inside one shared batch; draft, not executed."""
import asyncio

import pytest

from app.services.runtime_auth_batch import RuntimeAuthBatch, RuntimeAuthKey


def test_same_batch_early_timeout_does_not_cancel_later_consumer():
    async def run():
        reader = RuntimeAuthBatch(None)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        cancelled = False

        async def fetch(keys):
            nonlocal cancelled
            calls.append(keys)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise
            return {identity: identity for identity in keys}

        reader._fetch = fetch
        early_key = RuntimeAuthKey("u1", "sid1", "session1")
        late_key = RuntimeAuthKey("u2", "sid2", "session2")
        started = asyncio.get_running_loop().time()
        early = asyncio.create_task(reader.read(early_key, started + .1))
        late = asyncio.create_task(reader.read(late_key, started + 1))
        try:
            await entered.wait()
            assert calls == [(early_key, late_key)], "the test must exercise one shared batch"
            with pytest.raises(TimeoutError):
                await early
            assert not cancelled and not late.done()
            release.set()
            assert await late == late_key
            assert not cancelled and len(calls) == 1
            assert reader._pending == [] and reader._current == ()
        finally:
            release.set()
            await reader.close()
            await asyncio.gather(early, late, return_exceptions=True)

    async def bounded():
        async with asyncio.timeout(3):
            await run()
    asyncio.run(bounded())
