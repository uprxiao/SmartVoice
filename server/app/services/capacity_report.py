"""Read an operator-deployed summary; absent attested resource identity is unverified."""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import re
import stat

HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
MAX_SAFE = 9_007_199_254_740_991
FIELDS = {
    "report_id", "tested_at", "target_sessions", "environment_fingerprint", "app_commit",
    "config_sha256", "fixture_manifest_sha256", "hardware_model_manifest_sha256", "evidence_sha256",
    "normal_completed", "normal_total", "rejected", "failed", "fallback",
    "stop_to_result_p50_ms", "stop_to_result_p95_ms",
    "desktop_stop_to_readback_p50_ms", "desktop_stop_to_readback_p95_ms", "soak_passed",
}
COUNTS = FIELDS - {"report_id", "tested_at", "environment_fingerprint", "app_commit", "config_sha256",
                  "fixture_manifest_sha256", "hardware_model_manifest_sha256", "evidence_sha256", "soak_passed"}


@contextmanager
def _confined_stream(resolved: Path):
    # Resolve alone is not a containment fence: an ancestor could be replaced
    # by a symlink before open(). Walk from / using held directory descriptors
    # and refuse symlinks in every component, including the final leaf.
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("confined report lookup unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    with ExitStack() as owned:
        parent = os.open(resolved.anchor, flags | os.O_DIRECTORY)
        owned.callback(os.close, parent)
        for part in resolved.parts[1:-1]:
            parent = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            owned.callback(os.close, parent)
        fd = os.open(resolved.name, flags, dir_fd=parent)
        owned.callback(os.close, fd)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield stream


def validate_summary(value, current_fingerprint: str | None):
    unknown = {"status": "unverified", "report": None}
    if not isinstance(value, dict) or set(value) != FIELDS:
        return unknown
    if any(type(value[key]) is not int or not 0 <= value[key] <= MAX_SAFE for key in COUNTS):
        return unknown
    if not isinstance(value["report_id"], str) or not 1 <= len(value["report_id"]) <= 64:
        return unknown
    if any(ord(c) < 32 for c in value["report_id"]):
        return unknown
    for key in ("environment_fingerprint", "config_sha256", "fixture_manifest_sha256",
                "hardware_model_manifest_sha256", "evidence_sha256"):
        if not isinstance(value[key], str) or not HEX64.fullmatch(value[key]):
            return unknown
    if not isinstance(value["app_commit"], str) or not HEX40.fullmatch(value["app_commit"]):
        return unknown
    try:
        tested = dt.datetime.fromisoformat(value["tested_at"].replace("Z", "+00:00"))
        if tested.tzinfo is None or tested.utcoffset() != dt.timedelta(0) or tested > dt.datetime.now(dt.timezone.utc):
            return unknown
    except (TypeError, ValueError, AttributeError):
        return unknown
    target = value["target_sessions"]
    if not 1 <= target <= 2_147_483_647 or value["normal_total"] != 80 * target:
        return unknown
    if (value["normal_completed"] != value["normal_total"] or
        any(value[key] != 0 for key in ("rejected", "failed", "fallback")) or value["soak_passed"] is not True):
        return unknown
    for prefix in ("stop_to_result", "desktop_stop_to_readback"):
        p50, p95 = value[prefix + "_p50_ms"], value[prefix + "_p95_ms"]
        if not 0 <= p50 <= p95 or p50 > 2000 or p95 > 5000:
            return unknown
    # This value must come from a separately verified runtime/resource manifest,
    # never from this report, an administrator HTTP parameter, or copied config.
    if not isinstance(current_fingerprint, str) or not HEX64.fullmatch(current_fingerprint):
        return unknown
    return {"status": "valid" if current_fingerprint == value["environment_fingerprint"] else "stale", "report": value}


def load_capacity_report(path: str | None, current_fingerprint: str | None, *, allowed_roots=None):
    unknown = {"status": "unverified", "report": None}
    if not path:
        return unknown
    roots = allowed_roots or (Path(__file__).resolve().parents[2] / "config", Path("/etc/smartvoiceinput"))
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(__file__).resolve().parents[2] / candidate
    try:
        if candidate.is_symlink():
            return unknown
        resolved = candidate.resolve(strict=True)
        if not any(resolved.is_relative_to(Path(root).resolve()) for root in roots):
            return unknown
        # Reject special files without waiting for a FIFO peer before fstat.
        # fstat remains the authority on the opened file, including path races.
        with _confined_stream(resolved) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65_536:
                return unknown
            data = stream.read(65_537)
            if len(data) > 65_536:
                return unknown
        return validate_summary(json.loads(data), current_fingerprint)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return unknown
