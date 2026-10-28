"""Batch lifecycle drafts: fake only the database boundary, never auth policy."""
import asyncio

import pytest

from app.services.runtime_auth_batch import RuntimeAuthBatch, RuntimeAuthKey, RuntimeAuthUnavailable


def run_bounded(coroutine):
    async def bounded():
        # Test anti-hang guard only; production deadlines remain unchanged.
        async with asyncio.timeout(3):
            await coroutine
    asyncio.run(bounded())


def key(index):
    return RuntimeAuthKey(f"user-{index}", f"login-{index}", f"session-{index}")


def test_same_tick_waiters_share_one_read_without_mixing_results():
    async def run():
        reader = RuntimeAuthBatch(None)
        calls = []
        async def fetch(keys):
            calls.append(keys)
            return {identity: (identity.user_id, identity.login_sid, identity.session_id) for identity in keys}
        reader._fetch = fetch
        try:
            deadline = asyncio.get_running_loop().time() + 1
            identities = [key(i) for i in range(100)]
            actual = await asyncio.gather(*(reader.read(identity, deadline) for identity in identities))
            assert actual == [(identity.user_id, identity.login_sid, identity.session_id) for identity in identities]
            assert len(calls) == 1 and set(calls[0]) == set(identities)
        finally:
            await reader.close()
    run_bounded(run())


def test_cancel_one_waiter_does_not_cancel_other_users_query():
    async def run():
        reader = RuntimeAuthBatch(None)
        entered, release = asyncio.Event(), asyncio.Event()
        cancelled = False
        async def fetch(keys):
            nonlocal cancelled
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise
            return {identity: identity for identity in keys}
        reader._fetch = fetch
        deadline = asyncio.get_running_loop().time() + 1
        first = asyncio.create_task(reader.read(key(1), deadline))
        second = asyncio.create_task(reader.read(key(2), deadline))
        try:
            await entered.wait()
            first.cancel()
            result = await asyncio.gather(first, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            assert not cancelled and not second.done()
            release.set()
            assert await second == key(2)
            assert not cancelled
        finally:
            release.set()
            await reader.close()
            await asyncio.gather(first, second, return_exceptions=True)
    run_bounded(run())


def test_last_consumer_cancellation_stops_query_but_preserves_next_batch():
    async def run():
        reader = RuntimeAuthBatch(None)
        entered, stopped = asyncio.Event(), asyncio.Event()
        queued = asyncio.Event()
        calls = []
        async def fetch(keys):
            calls.append(keys)
            if len(calls) == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            return {identity: identity for identity in keys}
        reader._fetch = fetch
        deadline = asyncio.get_running_loop().time() + 1
        first = asyncio.create_task(reader.read(key(1), deadline))
        second = None
        async def next_request():
            queued.set()
            return await reader.read(key(2), deadline)
        try:
            await entered.wait()
            second = asyncio.create_task(next_request())
            await queued.wait()
            first.cancel()
            assert isinstance((await asyncio.gather(first, return_exceptions=True))[0], asyncio.CancelledError)
            assert await second == key(2)
            assert stopped.is_set() and calls == [(key(1),), (key(2),)]
        finally:
            await reader.close()
            await asyncio.gather(*(t for t in (first, second) if t is not None), return_exceptions=True)
    run_bounded(run())


def test_original_deadline_includes_wait_behind_another_batch():
    async def run():
        reader = RuntimeAuthBatch(None)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def fetch(keys):
            calls.append(keys)
            entered.set()
            await release.wait()
            return {identity: identity for identity in keys}
        reader._fetch = fetch
        first = asyncio.create_task(reader.read(key(1), asyncio.get_running_loop().time() + 1))
        try:
            await entered.wait()
            # Simulate a caller with only 20ms of its original 1s budget left.
            with pytest.raises(TimeoutError):
                await reader.read(key(2), asyncio.get_running_loop().time() + .02)
            assert calls == [(key(1),)] and not first.done()
            release.set()
            assert await first == key(1)
        finally:
            release.set()
            await reader.close()
            await asyncio.gather(first, return_exceptions=True)
    run_bounded(run())


def test_close_fails_inflight_and_queued_waiters_and_stops_query():
    async def run():
        reader = RuntimeAuthBatch(None)
        entered, stopped, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def fetch(keys):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        reader._fetch = fetch
        deadline = asyncio.get_running_loop().time() + 1
        first = asyncio.create_task(reader.read(key(1), deadline))
        await entered.wait()
        async def next_request():
            queued.set()
            return await reader.read(key(2), deadline)
        second = asyncio.create_task(next_request())
        await queued.wait()
        await reader.close()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(outcome, RuntimeAuthUnavailable) for outcome in outcomes)
        assert stopped.is_set()
        with pytest.raises(RuntimeAuthUnavailable):
            await reader.read(key(3), asyncio.get_running_loop().time() + 1)
    run_bounded(run())


def test_close_waits_for_cancelled_query_cleanup_without_cancelling_it_twice():
    async def run():
        reader = RuntimeAuthBatch(None)
        entered, cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        interrupted = False
        async def fetch(keys):
            nonlocal interrupted
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    interrupted = True
                    raise
        reader._fetch = fetch
        request = asyncio.create_task(reader.read(key(1), asyncio.get_running_loop().time() + 1))
        closing = None
        try:
            await entered.wait()
            request.cancel()
            assert isinstance((await asyncio.gather(request, return_exceptions=True))[0], asyncio.CancelledError)
            await cleanup_started.wait()
            closing = asyncio.create_task(reader.close())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not interrupted, "shutdown cancelled an already-cancelling query during connection cleanup"
            assert not closing.done(), "close returned before database cleanup completed"
            release_cleanup.set()
            await closing
            assert not interrupted
        finally:
            release_cleanup.set()
            await reader.close()
            await asyncio.gather(*(task for task in (request, closing) if task is not None), return_exceptions=True)
    run_bounded(run())
