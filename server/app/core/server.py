"""Uvicorn shutdown: publish/drain application work before closing transport."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from uvicorn import Config, Server

from .service_watchdog import ServiceWatchdog

logger = logging.getLogger("smartvoiceinput.supervisor")


async def _join_owned(task: asyncio.Task) -> bool:
    """Defer caller cancellation without sending a second cancel to cleanup."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    await task  # Retrieve failures; a cancelled owner is not normal completion.
    return cancelled


class ManagedServer(Server):
    def __init__(self, config: Config, *, begin_shutdown: Callable[[], asyncio.Task],
                 fatal_reason: Callable[[], str | None] = lambda: None):
        super().__init__(config)
        self._begin_application_shutdown = begin_shutdown
        self._transport_shutdown_task: asyncio.Task | None = None
        self._fatal_reason = fatal_reason
        self._watchdog = ServiceWatchdog.from_environment()
        self._watchdog_task: asyncio.Task | None = None
        self.exit_code = 0

    def _observe_fatal_state(self) -> bool:
        if self._fatal_reason() is None:
            return False
        if not self.exit_code:
            logger.critical("service_recovery_requested reason=INSTANCE_LOCK_LOST exit_code=75")
        self.exit_code, self.should_exit = 75, True
        return True

    async def on_tick(self, counter: int) -> bool:
        if self._observe_fatal_state():
            return True
        return await super().on_tick(counter)

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started and not self._observe_fatal_state() and self._watchdog:
            self._watchdog.notify(b"READY=1\nWATCHDOG=1")
            self._watchdog_task = asyncio.create_task(self._notify_watchdog())

    async def _notify_watchdog(self):
        while not self._observe_fatal_state():
            await asyncio.sleep(self._watchdog.interval_seconds)
            if self._observe_fatal_state():
                return
            # Continue through normal shutdown; fenced cleanup must never feed it.
            self._watchdog.notify(b"WATCHDOG=1")

    async def serve(self, sockets=None):
        try:
            await super().serve(sockets=sockets)
        finally:
            if self._watchdog_task:
                self._watchdog_task.cancel()
                await asyncio.gather(self._watchdog_task, return_exceptions=True)

    async def shutdown(self, sockets=None):
        cancelled = False
        try:
            # Synchronous cutoff occurs before the first await and while HTTP
            # is still accepting requests. Existing guards now return real503.
            cancelled = await _join_owned(self._begin_application_shutdown())
        finally:
            # Uvicorn closes sockets and then invokes the original lifespan.
            # That lifespan rejoins the same drain before releasing DB/lock.
            if self._transport_shutdown_task is None:
                self._transport_shutdown_task = asyncio.create_task(super().shutdown(sockets=sockets))
            cancelled = await _join_owned(self._transport_shutdown_task) or cancelled
        if cancelled:
            raise asyncio.CancelledError
