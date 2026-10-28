from __future__ import annotations

import datetime as dt
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.deps import ApiError
from ..models import DictionaryState, DictionaryTerm
from ..schemas.dictionary import ContextOut, TermOut

CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_TERMS = 5000
MAX_ASR_HINT_TERMS = 100
MAX_ASR_HINT_CHARS = 2000
MAX_REFINE_TERMS = 50
MAX_REFINE_CHARS = 4000


async def _prompt_rows(db: AsyncSession, user_id: str) -> list[DictionaryTerm]:
    return list(
        (
            await db.scalars(
                select(DictionaryTerm)
                .where(
                    DictionaryTerm.user_id == user_id,
                    DictionaryTerm.deleted_at.is_(None),
                )
                .order_by(DictionaryTerm.norm.asc(), DictionaryTerm.id.asc())
            )
        ).all()
    )


async def prompt_snapshot(db: AsyncSession, user_id: str) -> list[dict]:
    """Caller holds User and DictionaryState; never persist this full snapshot."""
    return [{"id": row.id, "term": row.term, "norm": row.norm,
             "aliases": [a for a in row.aliases or [] if isinstance(a, str)],
             "contexts": [context_out(c).model_dump() for c in (row.contexts or [])[:3]],
             "updated_timestamp": row.updated_at.timestamp()}
            for row in await _prompt_rows(db, user_id)]


async def asr_hint_terms(db: AsyncSession, user_id: str) -> list[str]:
    from .dictionary_prompt import asr_hints
    return asr_hints(await prompt_snapshot(db, user_id))


async def refine_prompt_terms(db: AsyncSession, user_id: str, raw_text: str) -> list[dict]:
    from .dictionary_prompt import refine_hints
    return refine_hints(await prompt_snapshot(db, user_id), raw_text)


def norm_of(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def clean_text(value: str, label: str) -> str:
    if any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in value):
        raise ApiError(422, "VALIDATION_ERROR", f"{label}不能包含换行或控制字符")
    text = value.strip()
    if not 1 <= len(text) <= 64:
        raise ApiError(422, "VALIDATION_ERROR", f"{label}长度必须为 1–64 个字符")
    return text


def validate_create(term: str, aliases: list[str]) -> tuple[str, str, list[str], list[str]]:
    term = clean_text(term, "词条")
    norm = norm_of(term)
    if not norm:
        raise ApiError(422, "VALIDATION_ERROR", "词条无效")
    if len(aliases) > 10:
        raise ApiError(422, "VALIDATION_ERROR", "别名最多 10 个")
    cleaned: list[str] = []
    norms: list[str] = []
    for alias in aliases:
        value = clean_text(alias, "别名")
        alias_norm = norm_of(value)
        if alias_norm == norm:
            raise ApiError(422, "VALIDATION_ERROR", "别名不能与词条相同")
        if alias_norm in norms:
            continue
        cleaned.append(value)
        norms.append(alias_norm)
    return term, norm, cleaned, norms


async def get_state(db: AsyncSession, user_id: str) -> DictionaryState:
    state = await db.get(DictionaryState, user_id)
    if state is None:
        await db.execute(insert(DictionaryState).values(user_id=user_id, version=0)
                         .on_conflict_do_nothing(index_elements=[DictionaryState.user_id]))
        state = await db.get(DictionaryState, user_id)
    return state


async def lock_state(db: AsyncSession, user_id: str) -> DictionaryState:
    """Caller must already hold User; readers may keep using get_state."""
    await db.execute(insert(DictionaryState).values(user_id=user_id, version=0)
                     .on_conflict_do_nothing(index_elements=[DictionaryState.user_id]))
    return await db.scalar(select(DictionaryState).where(DictionaryState.user_id == user_id)
                           .with_for_update().execution_options(populate_existing=True))


async def bump_version(db: AsyncSession, user_id: str) -> int:
    state = await get_state(db, user_id)
    state.version += 1
    state.updated_at = dt.datetime.now(dt.timezone.utc)
    return state.version


async def active_count(db: AsyncSession, user_id: str) -> int:
    from sqlalchemy import func

    return int(
        await db.scalar(
            select(func.count(DictionaryTerm.id)).where(
                DictionaryTerm.user_id == user_id,
                DictionaryTerm.deleted_at.is_(None),
            )
        )
        or 0
    )


def context_out(raw) -> ContextOut:
    if not isinstance(raw, dict):
        return ContextOut(alias_norm="")
    return ContextOut(
        alias_norm=str(raw.get("alias_norm", "")),
        before=str(raw.get("before", ""))[:80],
        after=str(raw.get("after", ""))[:80],
        left=str(raw.get("left", ""))[:80],
        right=str(raw.get("right", ""))[:80],
    )


async def ambiguity_index(db: AsyncSession, user_id: str) -> dict[str, set[str]]:
    """Map normalized alias/term -> display names of terms owning it."""
    rows = (
        await db.scalars(
            select(DictionaryTerm).where(
                DictionaryTerm.user_id == user_id,
                DictionaryTerm.deleted_at.is_(None),
            )
        )
    ).all()
    index: dict[str, set[str]] = {}
    for row in rows:
        index.setdefault(row.norm, set()).add(row.term)
        for alias in row.aliases or []:
            if isinstance(alias, str):
                index.setdefault(norm_of(alias), set()).add(row.term)
    return index


def term_out(
    term: DictionaryTerm,
    dictionary_version: int,
    index: dict[str, set[str]] | None = None,
) -> TermOut:
    index = index or {}
    ambiguous: list[str] = []
    for alias in term.aliases or []:
        if not isinstance(alias, str):
            continue
        alias_norm = norm_of(alias)
        owners = index.get(alias_norm, set())
        if any(norm_of(owner) != alias_norm for owner in owners) or len(owners) > 1:
            ambiguous.append(alias)
    return TermOut(
        id=term.id,
        term=term.term,
        norm=term.norm,
        aliases=[a for a in (term.aliases or []) if isinstance(a, str)],
        contexts=[context_out(item) for item in (term.contexts or [])][:3],
        scope=term.scope,
        source=term.source,
        version=term.version,
        dictionary_version=dictionary_version,
        created_at=term.created_at,
        updated_at=term.updated_at,
        ambiguous_aliases=ambiguous,
    )


async def get_active_term(
    db: AsyncSession, user_id: str, term_id: str
) -> DictionaryTerm:
    term = await db.scalar(
        select(DictionaryTerm).where(
            DictionaryTerm.user_id == user_id,
            DictionaryTerm.id == term_id,
            DictionaryTerm.deleted_at.is_(None),
        )
    )
    if term is None:
        raise ApiError(404, "NOT_FOUND", "词条不存在")
    return term


async def find_by_norm(
    db: AsyncSession, user_id: str, norm: str, exclude_id: str | None = None
) -> DictionaryTerm | None:
    query = select(DictionaryTerm).where(
        DictionaryTerm.user_id == user_id,
        DictionaryTerm.norm == norm,
        DictionaryTerm.deleted_at.is_(None),
    )
    if exclude_id:
        query = query.where(DictionaryTerm.id != exclude_id)
    return await db.scalar(query)
