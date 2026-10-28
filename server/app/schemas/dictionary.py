from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from .common import ApiModel, UUIDString


class ContextOut(ApiModel):
    alias_norm: str
    before: str = ""
    after: str = ""
    left: str = ""
    right: str = ""


class TermOut(ApiModel):
    id: str
    term: str
    norm: str
    aliases: list[str]
    contexts: list[ContextOut]
    scope: str = "default"
    source: str
    version: int
    dictionary_version: int = 0
    created_at: datetime
    updated_at: datetime
    ambiguous_aliases: list[str] = Field(default_factory=list)


class TermCreateIn(ApiModel):
    term: str = Field(min_length=1, max_length=64)
    aliases: list[str] = Field(default_factory=list, max_length=10)
    scope: str = "default"


class TermPatchIn(ApiModel):
    version: int = Field(ge=1)
    term: str | None = Field(default=None, min_length=1, max_length=64)
    aliases: list[str] | None = Field(default=None, max_length=10)


class TermDeleteTargetIn(ApiModel):
    id: UUIDString
    version: int = Field(ge=1, strict=True)


class TermBatchDeleteIn(ApiModel):
    items: list[TermDeleteTargetIn] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def unique_targets(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("批量删除不能包含重复词条")
        return self


class ImportRowOut(ApiModel):
    row: int
    status: str
    term_id: str | None = None
    error_code: str | None = None


class ImportResultOut(ApiModel):
    import_id: str
    created: int
    skipped: int
    conflicts: int
    invalid: int
    dictionary_version: int
    rows: list[ImportRowOut]
    replayed: bool = False
