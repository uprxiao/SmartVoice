"""Clock aligned with asyncio timeouts.

uvloop's ``loop.time()`` does not share the origin of ``time.monotonic()``
(the observed gap on macOS is thousands of seconds), so a deadline built from
``time.monotonic()`` and passed to ``asyncio.timeout_at`` expires immediately
under uvloop. Build every asyncio deadline through this helper instead.
"""
from __future__ import annotations

import asyncio


def loop_time() -> float:
    return asyncio.get_running_loop().time()
