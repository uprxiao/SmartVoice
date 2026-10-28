"""Deterministic control-flow regressions; not real-provider/CAP20 acceptance."""
import asyncio
import unittest

from app.core.clock import loop_time
from app.services.inference_scheduler import SchedulerBusy, Superseded, WeightedScheduler


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler = WeightedScheduler(1, 10, 10)
        self.tasks = []

    async def asyncTearDown(self):
        await self.scheduler.close()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def submit(self, owner, priority, operation, **kwargs):
        task = asyncio.create_task(self.scheduler.submit(owner, priority, operation,
            deadline=loop_time() + 2, **kwargs))
        self.tasks.append(task)
        return task

    async def test_actual_dispatch_is_three_high_to_one_low(self):
        order, release = [], asyncio.Event()
        async def blocker(slot):
            order.append("block")
            await release.wait()
        self.submit("block", "high", blocker)
        await until(lambda: order == ["block"])
        def operation(name):
            async def run(slot):
                order.append(name)
            return run
        for name in ("h0", "h1", "h2", "h3", "h4"):
            self.submit(name, "high", operation(name))
        for name in ("l0", "l1"):
            self.submit(name, "low", operation(name))
        await until(lambda: self.scheduler.snapshot()["high_waiting"] == 5)
        release.set()
        await asyncio.gather(*self.tasks)
        self.assertEqual(order, ["block", "h0", "h1", "l0", "h2", "h3", "h4", "l1"])

    async def test_partial_replace_preserves_fifo_and_releases_old_payload(self):
        release, started, order = asyncio.Event(), asyncio.Event(), []
        async def block(slot):
            started.set()
            await release.wait()
        self.submit("block", "high", block)
        await started.wait()
        def op(name):
            async def run(slot):
                order.append(name)
            return run
        old = self.submit("a", "low", op("old"))
        self.submit("b", "low", op("b"))
        await until(lambda: len(self.scheduler.queues["low"]) == 2)
        old_job = self.scheduler.queues["low"]["a"]
        latest = self.submit("a", "low", op("latest"), replace=True)
        with self.assertRaises(Superseded):
            await old
        self.assertIsNone(old_job.operation)
        self.assertEqual(list(self.scheduler.queues["low"]), ["a", "b"])
        release.set()
        await latest
        await asyncio.gather(*(t for t in self.tasks if t is not old))
        self.assertEqual(order, ["latest", "b"])

    async def test_cancel_holds_permit_until_operation_cleanup(self):
        started, cancel_seen, cleanup_release, second_started = [asyncio.Event() for _ in range(4)]
        async def operation(slot):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancel_seen.set()
                await cleanup_release.wait()
                raise
        first = self.submit("a", "high", operation)
        await started.wait()
        first.cancel()
        await cancel_seen.wait()
        async def second(slot):
            second_started.set()
        next_task = self.submit("b", "high", second)
        await asyncio.sleep(0)
        self.assertFalse(second_started.is_set())
        self.assertFalse(first.done())
        cleanup_release.set()
        await asyncio.gather(first, return_exceptions=True)
        await next_task
        self.assertTrue(second_started.is_set())

    async def test_queue_expiry_never_starts_operation(self):
        release, started, calls = asyncio.Event(), asyncio.Event(), []
        async def block(slot):
            started.set()
            await release.wait()
        self.submit("a", "high", block)
        await started.wait()
        async def expired(slot):
            calls.append("unexpected")
        queued = self.submit("b", "low", expired, wait_deadline=loop_time() + .02)
        with self.assertRaises(SchedulerBusy):
            await queued
        self.assertEqual(calls, [])
        self.assertEqual(self.scheduler.snapshot()["low_waiting"], 0)
        release.set()

    async def test_same_owner_never_runs_concurrently_across_priority_queues(self):
        started, release, order = asyncio.Event(), asyncio.Event(), []
        await self.scheduler.close()
        self.scheduler = WeightedScheduler(2, 10, 10)
        async def first(slot):
            order.append("first-start")
            started.set()
            await release.wait()
            order.append("first-end")
        async def finish(slot):
            order.append("finish")
        self.submit("same", "low", first)
        await started.wait()
        later = self.submit("same", "high", finish)
        await asyncio.sleep(.01)
        self.assertEqual(order, ["first-start"])
        release.set()
        await later
        self.assertEqual(order, ["first-start", "first-end", "finish"])
