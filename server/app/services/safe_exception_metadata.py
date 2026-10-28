"""Failure-only scalar metadata. Never format or retain an exception/frame object."""
from __future__ import annotations

import json
import logging
import re

_LOGGER = logging.getLogger(__name__)
_PHASES = frozenset(("process_sqlalchemy", "watchdog_auth_read", "auth_fetch_failed", "auth_batch_failed"))
_CODES = frozenset(("INTERNAL_ERROR", "AUTH_INVALID"))
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
_FILE = re.compile(r"[A-Za-z0-9_.-]{1,96}\Z")
_FRAME_LIMIT = 8
_BATCH_LIMIT = 8
_CHAIN_LIMIT = 3
_TRACEBACK_SCAN_LIMIT = 64
_AUTH_TIMEOUT_PHASES = _PHASES - {"process_sqlalchemy"}


def _session(value):
    return value if type(value) is str and _UUID.fullmatch(value) else None



def _chain_tail_frames(error):
    """Last eight of at most 64 visited nodes; never claim an unvisited tail."""
    cursor = frame = compiled = None
    try:
        cursor = BaseException.__getattribute__(error, "__traceback__")
        frames, visited = [], 0
        for _ in range(_TRACEBACK_SCAN_LIMIT):
            if cursor is None:
                break
            frame = cursor.tb_frame
            compiled = frame.f_code
            basename = compiled.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
            function = compiled.co_name
            frames.append({
                "basename": basename if _FILE.fullmatch(basename) else "unknown",
                "line": cursor.tb_lineno if type(cursor.tb_lineno) is int and cursor.tb_lineno > 0 else 0,
                "function": function if _NAME.fullmatch(function) or function in
                    ("<module>", "<lambda>", "<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>") else "unknown",
            })
            if len(frames) > _FRAME_LIMIT:
                del frames[0]
            visited += 1
            cursor = cursor.tb_next
        return {"frames": frames, "frames_truncated": visited > _FRAME_LIMIT or cursor is not None,
                "traceback_scan_truncated": cursor is not None}
    finally:
        del cursor, frame, compiled, error


def _auth_timeout_chain(error):
    """Only scalar metadata; native exception slots avoid custom attribute hooks."""
    current, successor = error, None
    result = {"exception_chain": [], "exception_chain_truncated": False,
              "exception_chain_cycle_detected": False, "exception_chain_unavailable": False}
    try:
        seen = {id(error)}
        for depth in range(_CHAIN_LIMIT + 1):
            relation = "cause"
            successor = BaseException.__getattribute__(current, "__cause__")
            if successor is None:
                if BaseException.__getattribute__(current, "__suppress_context__"):
                    break
                successor = BaseException.__getattribute__(current, "__context__")
                relation = "context"
            if successor is None:
                break
            if id(successor) in seen:
                result["exception_chain_cycle_detected"] = True
                break
            if depth == _CHAIN_LIMIT:
                result["exception_chain_truncated"] = True
                break
            seen.add(id(successor))
            kind = type(successor).__name__
            result["exception_chain"].append({
                "relation": relation,
                "exception_type": kind if _NAME.fullmatch(kind) else "UnknownException",
                **_chain_tail_frames(successor),
            })
            current, successor = successor, None
        return result
    except BaseException:
        # A metadata failure must not remove the original single log attempt.
        result["exception_chain_unavailable"] = True
        return result
    finally:
        del current, successor, error


def log_exception_metadata(error: BaseException, *, phase: str, code=None,
                           session_id=None, attempt_no=None) -> None:
    """Best effort only; no await, DB, request/call guessing, or exception text."""
    cursor = frame = compiled = None
    try:
        if phase not in _PHASES:
            return
        kind = type(error).__name__
        frames = []
        cursor = error.__traceback__
        # First eight existing traceback nodes: traversal itself is bounded.
        for _ in range(_FRAME_LIMIT):
            if cursor is None:
                break
            frame = cursor.tb_frame
            compiled = frame.f_code
            filename = compiled.co_filename
            basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
            function = compiled.co_name
            frames.append({
                "basename": basename if _FILE.fullmatch(basename) else "unknown",
                "line": cursor.tb_lineno if type(cursor.tb_lineno) is int and cursor.tb_lineno > 0 else 0,
                "function": function if _NAME.fullmatch(function) or function in
                    ("<module>", "<lambda>", "<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>") else "unknown",
            })
            cursor = cursor.tb_next
        payload = {
            "event": "RUNTIME_SAFE_EXCEPTION",
            "phase": phase,
            "code": code if code in _CODES else None,
            "session_id": _session(session_id),
            "attempt_no": attempt_no if type(attempt_no) is int and 0 < attempt_no <= 2_147_483_647 else None,
            "exception_type": kind if _NAME.fullmatch(kind) else "UnknownException",
            "frames": frames,
        }
        if phase in _AUTH_TIMEOUT_PHASES and isinstance(error, TimeoutError):
            payload.update(_auth_timeout_chain(error))
        # A ready scalar-only JSON string, never exception/trace objects as args/extras.
        _LOGGER.warning(json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                        exc_info=None, stack_info=False)
    except BaseException:
        # Even a broken logger/filter cannot replace the original failure path.
        return
    finally:
        # Do not mutate the original exception/traceback; just drop our frame refs.
        del cursor, frame, compiled


def log_batch_exception_metadata(error: BaseException, *, phase: str, waiters) -> None:
    """At most eight original waiter-session labels; attempt/call/request unknown."""
    try:
        if phase not in ("auth_fetch_failed", "auth_batch_failed"):
            return
        seen = set()
        # Inspect at most eight original entries, not an unbounded lazy iterable.
        for waiter in waiters[:_BATCH_LIMIT]:
            session_id = _session(waiter.key.session_id)
            if session_id is not None and session_id not in seen:
                seen.add(session_id)
                log_exception_metadata(error, phase=phase, session_id=session_id)
        if not seen:
            log_exception_metadata(error, phase=phase)
    except BaseException:
        return
