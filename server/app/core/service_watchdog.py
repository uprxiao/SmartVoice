"""Process-local systemd watchdog; no notifications outside the owning unit."""
from __future__ import annotations

import logging
import os
import socket

logger = logging.getLogger("smartvoiceinput.supervisor")


class ServiceWatchdog:
    def __init__(self, address: str, timeout_seconds: float):
        self.address = "\0" + address[1:] if address.startswith("@") else address
        self.interval_seconds = min(5.0, timeout_seconds / 3)

    @classmethod
    def from_environment(cls):
        address = os.environ.get("NOTIFY_SOCKET")
        if not address:
            return None
        try:
            owner = int(os.environ.get("WATCHDOG_PID", str(os.getpid())))
            timeout_us = int(os.environ.get("WATCHDOG_USEC", "0"))
            if owner != os.getpid() or timeout_us <= 0 or not address.startswith(("/", "@")):
                return None
            return cls(address, timeout_us / 1_000_000)
        except (ValueError, OverflowError):
            logger.error("systemd_watchdog_configuration_invalid")
            return None

    def notify(self, message: bytes) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                channel.setblocking(False)
                channel.sendto(message, self.address)
            return True
        except OSError as exc:
            # No socket address or exception message: both may contain private paths.
            logger.error("systemd_notify_failed error_type=%s", type(exc).__name__)
            return False
