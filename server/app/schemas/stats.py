from __future__ import annotations

from pydantic import Field

from .common import ApiModel


class DailyStat(ApiModel):
    date: str
    sessions: int = 0
    chars: int = 0
    successful_audio_ms: int = 0
    heat_level: int = 0


class StatTotals(ApiModel):
    sessions: int = 0
    received_audio_ms: int = 0
    result_sessions: int = 0
    failed_sessions: int = 0
    empty_sessions: int = 0
    cancelled_sessions: int = 0
    chars: int = 0
    successful_audio_ms: int = 0
    average_chars_per_minute: float = 0.0
    fallback_sessions: int = 0
    inserted_sessions: int = 0
    processing_success_rate: float = 0.0
    insertion_success_rate: float = 0.0
    fallback_rate: float = 0.0
    learning_events: int = 0


class StatsOut(ApiModel):
    timezone: str
    date_from: str
    date_to: str
    totals: StatTotals
    active_days_in_range: int = 0
    current_streak_days: int = 0
    longest_streak_days: int = 0
    dictionary_terms: int = 0
    cumulative_learning_events: int = 0
    daily: list[DailyStat] = Field(default_factory=list)


class ModelUsageOut(ApiModel):
    date: str
    provider: str
    model_id: str
    purpose: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    unknown_calls: int = 0
    elapsed_ms: int = 0
    processed_audio_ms: int = 0
    prompt_tokens_known: int = 0
    completion_tokens_known: int = 0
    prompt_tokens_unknown_calls: int = 0
    completion_tokens_unknown_calls: int = 0
