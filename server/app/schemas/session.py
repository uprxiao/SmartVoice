from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .common import ApiModel, UUIDString


class ResultOut(ApiModel):
    result_id: str
    attempt_no: int
    attempt_kind: str
    raw_text: str
    refined_text: str
    review_required: bool
    review_reason: str | None = None
    text_hash: str
    dictionary_version: int
    stop_to_result_ms: int
    created_at: datetime


class SessionOut(ApiModel):
    session_id: str
    mode: str
    state: str
    current_attempt_no: int
    learning_enabled: bool
    current_result_id: str | None = None
    audio_complete: bool
    received_samples: int
    audio_ms: int
    # Recording limit fixed at creation (V2 §4.1); None for pre-V2 sessions.
    max_record_seconds: int | None = None
    created_at: datetime
    first_terminal_at: datetime | None = None
    content_expires_at: datetime | None = None
    content_expired: bool = False
    error_code: str | None = None
    result: ResultOut | None = None
    retry_kind: str = "none"
    retry_expires_at: datetime | None = None
    retry_unavailable_reason: str | None = None
    insertion_status: str | None = None
    replayed: bool = False


class HistoryOut(ApiModel):
    items: list[SessionOut]
    next_cursor: str | None


class SessionCreateIn(ApiModel):
    client_request_id: UUIDString
    mode: Literal["dictation"] = "dictation"
    learning_enabled: bool = True


class InsertionOut(ApiModel):
    insertion_id: str
    session_id: str
    result_id: str
    status: str
    observable: bool
    error_code: str | None = None
    claimed_at: datetime
    completed_at: datetime | None = None
    replayed: bool = False


class InsertionClaimIn(ApiModel):
    result_id: UUIDString
    insertion_id: UUIDString
    text_hash: str = Field(min_length=64, max_length=64)
    review_confirmed: bool = False


class InsertionReceiptIn(ApiModel):
    status: Literal["inserted", "failed", "unverified"]
    observable: bool = False
    error_code: str | None = None


class CorrectionIn(ApiModel):
    session_id: UUIDString
    insertion_id: UUIDString
    correction_id: UUIDString
    offset: int = Field(ge=0, le=32_000)
    before: str = Field(min_length=1, max_length=32)
    after: str = Field(min_length=1, max_length=32)
    left: str = Field(default="", max_length=80)
    right: str = Field(default="", max_length=80)


class EmptyIn(ApiModel):
    pass


class RetryIn(ApiModel):
    retry_kind: Literal["asr", "refine"]
