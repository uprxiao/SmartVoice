"""Uvicorn shutdown: publish/drain application work before closing transport."""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from uvicorn import Config, Server


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
    def __init__(self, config: Config, *, begin_shutdown: Callable[[], asyncio.Task]):
        super().__init__(config)
        self._begin_application_shutdown = begin_shutdown
        self._transport_shutdown_task: asyncio.Task | None = None

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
