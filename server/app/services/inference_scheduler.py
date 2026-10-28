"""Single-loop bounded 3:1 scheduler; dispatch counts actual starts, not polls.

Each owner has at most one running job and one queued job per class. A partial
replacement keeps the owner's queue position. All cancellation waits for the
operation's cleanup before returning its execution permit.
"""
from __future__ import annotations

import asyncio
import contextvars
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Hashable


class SchedulerBusy(RuntimeError):
    pass


class Superseded(RuntimeError):
    pass


@dataclass(eq=False)
class Job:
    owner: Hashable
    priority: str
    operation: Callable[[int], Awaitable]
    deadline: float
    wait_deadline: float
    future: asyncio.Future
    context: contextvars.Context
    task: asyncio.Task | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    started: bool = False
    cancel_requested: bool = False
    wait_timer: asyncio.TimerHandle | None = None


class WeightedScheduler:
    def __init__(self, slots: int, high_limit: int, low_limit: int):
        if min(slots, high_limit, low_limit) < 1:
            raise ValueError("scheduler bounds must be positive")
        self.slots = slots
        self.limits = {"high": high_limit, "low": low_limit}
        self.queues = {"high": OrderedDict(), "low": OrderedDict()}
        self.running: dict[Hashable, Job] = {}
        self.workers: list[asyncio.Task] = []
        self.wake = asyncio.Event()
        self.high_turns = 0
        self.closed = False

    def snapshot(self) -> dict:
        return {"running": len(self.running),
                "high_waiting": len(self.queues["high"]),
                "low_waiting": len(self.queues["low"])}

    async def submit(self, owner, priority, operation, *, deadline: float,
                     wait_deadline: float | None = None, replace: bool = False):
        now = asyncio.get_running_loop().time()
        if self.closed:
            raise SchedulerBusy("scheduler closed")
        if priority not in self.queues:
            raise ValueError("invalid priority")
        if deadline <= now:
            raise TimeoutError("execution deadline elapsed")
        wait_deadline = min(deadline, wait_deadline or deadline)
        queue = self.queues[priority]
        old = queue.get(owner)
        if old is not None:
            if not replace:
                raise SchedulerBusy("owner already queued")
            if not old.future.done():
                old.future.set_exception(Superseded("newer window replaced pending window"))
            if old.wait_timer is not None:
                old.wait_timer.cancel()
            old.operation = None
            old.done.set()
        elif len(queue) >= self.limits[priority]:
            raise SchedulerBusy("queue full")
        loop = asyncio.get_running_loop()
        job = Job(owner, priority, operation, deadline, wait_deadline,
                  loop.create_future(), contextvars.copy_context())
        queue[owner] = job  # OrderedDict replacement preserves queue position.
        if wait_deadline < deadline:
            job.wait_timer = loop.call_at(wait_deadline, self._expire_queued, job)
        if not self.workers:
            self.workers = [asyncio.create_task(self._worker(i)) for i in range(self.slots)]
        self.wake.set()
        try:
            return await asyncio.wait_for(asyncio.shield(job.future), max(0, deadline - loop.time()))
        except (asyncio.CancelledError, TimeoutError):
            await self._cancel_job(job)
            raise

    def _expire_queued(self, job: Job) -> None:
        queue = self.queues[job.priority]
        if queue.get(job.owner) is not job:
            return
        queue.pop(job.owner)
        if not job.future.done():
            job.future.set_exception(SchedulerBusy("queue wait deadline elapsed"))
        job.operation = None
        job.done.set()
        self.wake.set()

    def _take(self) -> Job | None:
        now = asyncio.get_running_loop().time()
        for queue in self.queues.values():
            for key, job in list(queue.items()):
                if job.future.done() or job.wait_deadline <= now:
                    queue.pop(key, None)
                    if job.wait_timer is not None:
                        job.wait_timer.cancel()
                    if not job.future.done():
                        job.future.set_exception(SchedulerBusy("queue wait deadline elapsed"))
                    job.operation = None
                    job.done.set()
        order = ("high", "low") if self.high_turns < 3 else ("low", "high")
        for priority in order:
            queue = self.queues[priority]
            for key, job in list(queue.items()):
                if key in self.running:
                    continue
                queue.pop(key)
                if job.wait_timer is not None:
                    job.wait_timer.cancel()
                self.running[key] = job
                job.started = True
                self.high_turns = self.high_turns + 1 if priority == "high" else 0
                # Empty low queue may lend indefinitely, but gets the next turn
                # once it becomes nonempty after >=3 high dispatches.
                return job
        return None

    async def _worker(self, slot: int):
        while not self.closed:
            job = self._take()
            if job is None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=0.05)
                except TimeoutError:
                    pass
                continue
            try:
                job.task = asyncio.create_task(job.operation(slot), context=job.context)
                value = await job.task
                if not job.future.done():
                    job.future.set_result(value)
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.cancel()
            except Exception as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                self.running.pop(job.owner, None)
                job.operation = None  # release request/audio closure promptly
                job.task = None
                job.done.set()
                self.wake.set()

    async def _cancel_job(self, job: Job):
        if job.wait_timer is not None:
            job.wait_timer.cancel()
        queue = self.queues[job.priority]
        if queue.get(job.owner) is job:
            queue.pop(job.owner)
            job.operation = None
            job.done.set()
        if job.task is not None and not job.cancel_requested:
            job.cancel_requested = True
            job.task.cancel()
        if not job.future.done():
            job.future.cancel()
        await job.done.wait()
        self.wake.set()

    async def cancel(self, owner, *, queued_only: bool = False):
        jobs = [q[owner] for q in self.queues.values() if owner in q]
        if not queued_only and owner in self.running:
            jobs.append(self.running[owner])
        for job in jobs:
            await self._cancel_job(job)

    async def close(self):
        self.closed = True
        jobs = list(self.running.values()) + [j for q in self.queues.values() for j in q.values()]
        for job in jobs:
            await self._cancel_job(job)
        self.wake.set()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
