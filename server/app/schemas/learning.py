from __future__ import annotations

from datetime import datetime

from .common import ApiModel
from .dictionary import TermOut


class LearningEventOut(ApiModel):
    event_id: str
    correction_id: str
    status: str
    reason: str | None = None
    classification: str | None = None
    ambiguous: bool | None = None
    term: TermOut | None = None
    term_id: str | None = None
    after_version: int | None = None
    undone: bool = False
    undo_available: bool = False
    created_at: datetime
