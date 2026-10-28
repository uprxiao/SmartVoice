from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class AsrCapabilities:
    encoding: str = "pcm_s16le"
    sample_rate_hz: int = 16_000
    channels: int = 1
    stable_partial: bool = True
    backend_version: str = "unknown"
    provider_id: str = ""
    provider_version: str = ""
    supports_hints: bool = True
    # Catch-up send rate for providers that replay buffered audio; None = local.
    replay_rate: float | None = None
    max_record_seconds: int = 180
    max_concurrent_sessions: int | None = None
    supports_resume: bool = False


@dataclass
class AsrOpenOptions:
    session_id: str
    attempt_no: int
    language: str = "auto"
    hint_terms: list[str] = field(default_factory=list)
    max_samples: int = 2_880_000
    user_id: str = ""
    # Original attempt ASR operation budget, before failed-audio TTL clipping.
    # None preserves the old deadline-only provider/direct-call contract.
    outcome_deadline: float | None = None
    eligibility_deadline: float | None = None
    # Completed segments of a failed attempt (same provider/version only).
    resume: AsrResume | None = None


@dataclass
class AsrPartial:
    revision: int
    stable_text: str
    provisional_text: str
    decoded_samples: int
    received_samples: int


@dataclass
class AsrSegment:
    start_sample: int
    end_sample: int
    text: str


@dataclass(frozen=True)
class AsrResume:
    """Completed-segment text of a failed attempt; held with its audio lease only."""
    provider_id: str
    provider_version: str
    segments: tuple[AsrSegment, ...]

    @property
    def end_sample(self) -> int:
        return self.segments[-1].end_sample if self.segments else 0


@dataclass
class AsrFinal:
    text: str
    segments: list[AsrSegment]
    no_speech: bool
    model_id: str
    processed_samples: int


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 resume: AsrResume | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.resume = resume


class AsrSession(Protocol):
    async def feed(self, pcm_s16le: bytes, start_sample: int) -> None: ...
    def partials(self) -> AsyncIterator[AsrPartial]: ...
    async def finish(self, total_samples: int, deadline: float) -> AsrFinal: ...
    async def cancel(self) -> None: ...


class AsrProvider(Protocol):
    model_id: str
    capabilities: AsrCapabilities

    async def prepare(self) -> None: ...
    async def open(self, options: AsrOpenOptions) -> AsrSession: ...
    async def health(self) -> bool: ...
    async def close(self) -> None: ...
