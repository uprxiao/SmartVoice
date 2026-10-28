from __future__ import annotations

import csv
import hashlib
import io
import uuid

from fastapi import APIRouter, Depends, File, Header, Query, Request, Response, UploadFile
from sqlalchemy import or_, select, text

from ..models import DictionaryImport, DictionaryImportRow, DictionaryTerm
from ..schemas.dictionary import ImportResultOut, ImportRowOut, TermBatchDeleteIn, TermCreateIn, TermPatchIn
from ..services import dictionary_service as svc
from ..services.idempotency import begin, lock_user, require_key
from ..services.pagination import clamp_limit, decode_cursor, encode_cursor
from .deps import ApiError, AuthContext, require_auth

router = APIRouter(prefix="/api/v1/dictionary", tags=["dictionary"])


def _require_key(key: str | None) -> str:
    return require_key(key)


def _term_response(term, version, index, replayed: bool = False) -> dict:
    body = svc.term_out(term, version, index).model_dump(mode="json")
    body["replayed"] = replayed
    return body


@router.get("/terms")
async def list_terms(
    request: Request,
    q: str | None = Query(default=None, max_length=64),
    scope: str = "default",
    source: str = "all",
    cursor: str | None = None,
    limit: int | None = None,
    ctx: AuthContext = Depends(require_auth),
):
    if scope != "default":
        raise ApiError(422, "VALIDATION_ERROR", "scope 只支持 default")
    if source not in ("all", "manual", "auto"):
        raise ApiError(422, "VALIDATION_ERROR", "source 无效")
    if q is not None and not q.strip():
        q = None
    secret = request.app.state.config.idempotency_secret
    page_size = clamp_limit(limit)
    async with request.app.state.db.session_factory() as db:
        state = await svc.get_state(db, ctx.user.id)
        query = select(DictionaryTerm).where(
            DictionaryTerm.user_id == ctx.user.id,
            DictionaryTerm.deleted_at.is_(None),
        )
        if source != "all":
            query = query.where(DictionaryTerm.source == source)
        if q:
            literal = (
                q.strip()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{literal}%"
            query = query.where(
                or_(
                    DictionaryTerm.term.ilike(pattern, escape="\\"),
                    DictionaryTerm.norm.ilike(pattern, escape="\\"),
                    text(
                        "EXISTS (SELECT 1 FROM jsonb_array_elements_text("
                        "CAST(dictionary_terms.aliases AS jsonb)) AS alias_value "
                        "WHERE alias_value ILIKE :alias_pattern ESCAPE '\\')"
                    ),
                )
            ).params(alias_pattern=pattern)
        if cursor:
            payload = decode_cursor(secret, f"dict:{ctx.user.id}", cursor)
            if payload.get("v") != state.version:
                raise ApiError(409, "DICTIONARY_CHANGED", "词库已变化，请刷新")
            last_norm = payload.get("norm", "")
            last_id = payload.get("id", "")
            query = query.where(
                or_(
                    DictionaryTerm.norm > last_norm,
                    (DictionaryTerm.norm == last_norm) & (DictionaryTerm.id > last_id),
                )
            )
        rows = (
            await db.scalars(
                query.order_by(DictionaryTerm.norm.asc(), DictionaryTerm.id.asc()).limit(
                    page_size + 1
                )
            )
        ).all()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        index = await svc.ambiguity_index(db, ctx.user.id)
        items = [svc.term_out(row, state.version, index) for row in rows]
        next_cursor = None
        if has_more and rows:
            next_cursor = encode_cursor(
                secret,
                f"dict:{ctx.user.id}",
                {"v": state.version, "norm": rows[-1].norm, "id": rows[-1].id},
            )
        return {
            "items": items,
            "next_cursor": next_cursor,
            "dictionary_version": state.version,
        }


@router.post("/terms", status_code=201)
async def create_term(
    data: TermCreateIn,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_auth),
):
    key = _require_key(idempotency_key)
    if data.scope != "default":
        raise ApiError(422, "VALIDATION_ERROR", "scope 只支持 default")
    term, norm, aliases, _ = svc.validate_create(data.term, data.aliases)
    secret = request.app.state.config.idempotency_secret
    term_id = str(uuid.uuid4())
    async with request.app.state.db.session_factory() as db:
        await lock_user(db, ctx.user.id)
        await svc.lock_state(db, ctx.user.id)
        replayed, target_id = await begin(
            db,
            secret,
            ctx.user.id,
            "dictionary.term.create",
            key,
            {"term": term, "aliases": aliases, "scope": "default"},
            "dictionary_term",
            term_id,
        )
        if replayed:
            existing = await db.scalar(
                select(DictionaryTerm).where(
                    DictionaryTerm.user_id == ctx.user.id,
                    DictionaryTerm.id == target_id,
                )
            )
            if existing is None:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "幂等记录对应的词条不存在")
            state = await svc.get_state(db, ctx.user.id)
            index = await svc.ambiguity_index(db, ctx.user.id)
            response.status_code = 200
            return _term_response(existing, state.version, index, replayed=True)
        if await svc.find_by_norm(db, ctx.user.id, norm) is not None:
            raise ApiError(409, "TERM_EXISTS", "该词条已存在")
        if await svc.active_count(db, ctx.user.id) >= svc.MAX_TERMS:
            raise ApiError(409, "DICTIONARY_LIMIT", "词条数量已达上限")
        row = DictionaryTerm(
            id=term_id,
            user_id=ctx.user.id,
            term=term,
            norm=norm,
            aliases=aliases,
            contexts=[],
            scope="default",
            source="manual",
            version=1,
        )
        db.add(row)
        await db.flush()
        version = await svc.bump_version(db, ctx.user.id)
        await db.commit()
        await db.refresh(row)
        index = await svc.ambiguity_index(db, ctx.user.id)
        return _term_response(row, version, index, replayed=False)


@router.patch("/terms/{term_id}")
async def patch_term(
    term_id: str,
    data: TermPatchIn,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_auth),
):
    key = _require_key(idempotency_key)
    secret = request.app.state.config.idempotency_secret
    payload = {
        "version": data.version,
        "term": data.term,
        "aliases": data.aliases,
    }
    async with request.app.state.db.session_factory() as db:
        await lock_user(db, ctx.user.id)
        await svc.lock_state(db, ctx.user.id)
        replayed, _ = await begin(
            db,
            secret,
            ctx.user.id,
            "dictionary.term.patch",
            key,
            payload,
            "dictionary_term",
            term_id,
        )
        row = await svc.get_active_term(db, ctx.user.id, term_id)
        state = await svc.get_state(db, ctx.user.id)
        if replayed:
            index = await svc.ambiguity_index(db, ctx.user.id)
            return _term_response(row, state.version, index, replayed=True)
        if row.version != data.version:
            raise ApiError(
                409,
                "VERSION_CONFLICT",
                "词条已变化，请刷新后重试",
                details={"current_version": row.version},
            )
        changed = False
        new_term = row.term
        new_norm = row.norm
        if data.term is not None:
            new_term = svc.clean_text(data.term, "词条")
            new_norm = svc.norm_of(new_term)
            if new_norm != row.norm:
                existing = await svc.find_by_norm(
                    db, ctx.user.id, new_norm, exclude_id=term_id
                )
                if existing is not None:
                    raise ApiError(409, "TERM_EXISTS", "该词条已存在")
                if data.aliases is None and any(
                    isinstance(alias, str) and svc.norm_of(alias) == new_norm
                    for alias in (row.aliases or [])
                ):
                    raise ApiError(422, "VALIDATION_ERROR", "别名不能与词条相同")
        if new_term != row.term:
            changed = True
        if data.aliases is not None:
            _, _, new_aliases, _ = svc.validate_create(
                new_term, data.aliases
            )
            if new_aliases != list(row.aliases or []):
                changed = True
            if changed:
                allowed = {svc.norm_of(alias) for alias in new_aliases}
                row.contexts = [
                    item
                    for item in (row.contexts or [])
                    if isinstance(item, dict) and item.get("alias_norm") in allowed
                ]
                row.aliases = new_aliases
        if not changed:
            index = await svc.ambiguity_index(db, ctx.user.id)
            await db.commit()  # No-op writes still reserve their idempotency key.
            return _term_response(row, state.version, index, replayed=False)
        row.term = new_term
        row.norm = new_norm
        row.source = "manual"
        row.version += 1
        version = await svc.bump_version(db, ctx.user.id)
        await db.commit()
        await db.refresh(row)
        index = await svc.ambiguity_index(db, ctx.user.id)
        return _term_response(row, version, index, replayed=False)


@router.post("/terms/batch-delete")
async def batch_delete_terms(
    data: TermBatchDeleteIn,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_auth),
):
    key = _require_key(idempotency_key)
    targets = sorted(data.items, key=lambda item: item.id)
    ids = [item.id for item in targets]
    async with request.app.state.db.session_factory() as db:
        await lock_user(db, ctx.user.id)
        state = await svc.lock_state(db, ctx.user.id)
        replayed, _ = await begin(
            db, request.app.state.config.idempotency_secret, ctx.user.id,
            "dictionary.terms.batch-delete", key,
            {"items": [item.model_dump() for item in targets]}, "dictionary_batch", key,
        )
        rows = list((await db.scalars(select(DictionaryTerm).where(
            DictionaryTerm.user_id == ctx.user.id, DictionaryTerm.id.in_(ids),
        ).order_by(DictionaryTerm.id).with_for_update())).all())
        if len(rows) != len(targets) or (not replayed and any(row.deleted_at is not None for row in rows)):
            raise ApiError(404, "NOT_FOUND", "所选词条不存在，请刷新后重新选择")
        if not replayed:
            if any(row.version != item.version for row, item in zip(rows, targets)):
                raise ApiError(409, "VERSION_CONFLICT", "所选词条已变化，整批未删除，请刷新后重新选择")
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            for row in rows:
                row.deleted_at = now
                row.version += 1
                row.updated_at = now
            await svc.bump_version(db, ctx.user.id)
            await db.commit()
        return {"deleted_ids": ids, "dictionary_version": state.version, "replayed": replayed}


@router.delete("/terms/{term_id}")
async def delete_term(
    term_id: str,
    request: Request,
    version: int = Query(ge=1),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_auth),
):
    key = _require_key(idempotency_key)
    secret = request.app.state.config.idempotency_secret
    async with request.app.state.db.session_factory() as db:
        await lock_user(db, ctx.user.id)
        await svc.lock_state(db, ctx.user.id)
        replayed, _ = await begin(
            db,
            secret,
            ctx.user.id,
            "dictionary.term.delete",
            key,
            {"version": version},
            "dictionary_term",
            term_id,
        )
        row = await db.scalar(
            select(DictionaryTerm).where(
                DictionaryTerm.user_id == ctx.user.id,
                DictionaryTerm.id == term_id,
            )
        )
        if row is None:
            raise ApiError(404, "NOT_FOUND", "词条不存在")
        state = await svc.get_state(db, ctx.user.id)
        if replayed:
            return {
                "id": row.id,
                "deleted": row.deleted_at is not None,
                "version": row.version,
                "dictionary_version": state.version,
                "replayed": True,
            }
        if row.deleted_at is not None:
            raise ApiError(404, "NOT_FOUND", "词条不存在")
        if row.version != version:
            raise ApiError(
                409,
                "VERSION_CONFLICT",
                "词条已变化，请刷新后重试",
                details={"current_version": row.version},
            )
        from datetime import datetime, timezone

        row.deleted_at = datetime.now(timezone.utc)
        row.version += 1
        dictionary_version = await svc.bump_version(db, ctx.user.id)
        await db.commit()
        return {
            "id": row.id,
            "deleted": True,
            "version": row.version,
            "dictionary_version": dictionary_version,
            "replayed": False,
        }


MAX_CSV_BYTES = 1024 * 1024
MAX_CSV_ROWS = 1000


def _parse_csv(raw: bytes) -> list[tuple[int, str, list[str]]]:
    try:
        text_value = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ApiError(422, "VALIDATION_ERROR", "CSV 必须为 UTF-8 编码") from exc
    reader = csv.reader(io.StringIO(text_value, newline=""))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ApiError(422, "VALIDATION_ERROR", "CSV 缺少表头") from exc
    normalized = [cell.strip().lower() for cell in header]
    if len(set(normalized)) != len(normalized):
        raise ApiError(422, "VALIDATION_ERROR", "CSV 存在重复列名")
    if "term" not in normalized:
        raise ApiError(422, "VALIDATION_ERROR", "CSV 缺少 term 列")
    if any(name not in ("term", "aliases") for name in normalized):
        raise ApiError(422, "VALIDATION_ERROR", "CSV 包含未知列")
    term_index = normalized.index("term")
    alias_index = normalized.index("aliases") if "aliases" in normalized else None
    records: list[tuple[int, str, list[str]]] = []
    try:
        for row_no, cells in enumerate(reader, start=2):
            if not cells or all(not cell.strip() for cell in cells):
                continue
            if len(records) >= MAX_CSV_ROWS:
                raise ApiError(413, "REQUEST_TOO_LARGE", "CSV 数据行超过 1000")
            term = cells[term_index] if term_index < len(cells) else ""
            aliases: list[str] = []
            if alias_index is not None and alias_index < len(cells):
                aliases = [part.strip() for part in cells[alias_index].split(";") if part.strip()]
            records.append((row_no, term, aliases))
    except csv.Error as exc:
        raise ApiError(422, "VALIDATION_ERROR", "CSV 语法错误") from exc
    return records


def _import_result(
    import_id: str, rows: list[DictionaryImportRow], version: int, replayed: bool
) -> dict:
    counts = {"created": 0, "skipped": 0, "conflict": 0, "invalid": 0}
    for row in rows:
        if row.status in counts:
            counts[row.status] += 1
    return ImportResultOut(
        import_id=import_id,
        created=counts["created"],
        skipped=counts["skipped"],
        conflicts=counts["conflict"],
        invalid=counts["invalid"],
        dictionary_version=version,
        rows=[
            ImportRowOut(
                row=row.row_no,
                status=row.status,
                term_id=row.term_id,
                error_code=row.error_code,
            )
            for row in rows
        ],
        replayed=replayed,
    ).model_dump()


@router.post("/import")
async def import_csv(
    request: Request,
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_auth),
):
    key = _require_key(idempotency_key)
    raw = await file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise ApiError(413, "REQUEST_TOO_LARGE", "CSV 文件超过 1 MiB")
    digest = hashlib.sha256(raw).digest()
    records = _parse_csv(raw)
    secret = request.app.state.config.idempotency_secret
    import_id = str(uuid.uuid4())
    async with request.app.state.db.session_factory() as db:
        await lock_user(db, ctx.user.id)
        await svc.lock_state(db, ctx.user.id)
        replayed, target_id = await begin(
            db,
            secret,
            ctx.user.id,
            "dictionary.import",
            key,
            {"file_sha256": digest.hex()},
            "dictionary_import",
            import_id,
        )
        if replayed:
            existing = await db.scalar(
                select(DictionaryImport).where(
                    DictionaryImport.user_id == ctx.user.id,
                    DictionaryImport.id == target_id,
                )
            )
            if existing is None:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "幂等记录不存在")
            rows = (
                await db.scalars(
                    select(DictionaryImportRow)
                    .where(
                        DictionaryImportRow.user_id == ctx.user.id,
                        DictionaryImportRow.import_id == existing.id,
                    )
                    .order_by(DictionaryImportRow.row_no.asc())
                )
            ).all()
            state = await svc.get_state(db, ctx.user.id)
            return _import_result(existing.id, rows, state.version, True)
        if await svc.active_count(db, ctx.user.id) >= svc.MAX_TERMS:
            raise ApiError(409, "DICTIONARY_LIMIT", "词条数量已达上限")
        record = DictionaryImport(
            id=import_id,
            user_id=ctx.user.id,
            request_key=key,
            file_hash=digest,
            total_rows=len(records),
            state="processing",
        )
        db.add(record)
        await db.flush()
        result_rows: list[DictionaryImportRow] = []
        current_terms = await svc.active_count(db, ctx.user.id)
        for row_no, term_text, alias_texts in records:
            row = DictionaryImportRow(
                user_id=ctx.user.id,
                import_id=import_id,
                row_no=row_no,
                status="invalid",
            )
            try:
                term, norm, aliases, _ = svc.validate_create(term_text, alias_texts)
            except ApiError as exc:
                row.error_code = exc.code
                result_rows.append(row)
                db.add(row)
                continue
            if current_terms >= svc.MAX_TERMS:
                row.error_code = "DICTIONARY_LIMIT"
                result_rows.append(row)
                db.add(row)
                continue
            existing = await svc.find_by_norm(db, ctx.user.id, norm)
            if existing is not None:
                row.status = (
                    "skipped"
                    if sorted(svc.norm_of(a) for a in (existing.aliases or []) if isinstance(a, str))
                    == sorted(svc.norm_of(a) for a in aliases)
                    else "conflict"
                )
                row.term_id = existing.id
                result_rows.append(row)
                db.add(row)
                continue
            term_row = DictionaryTerm(
                user_id=ctx.user.id,
                term=term,
                norm=norm,
                aliases=aliases,
                contexts=[],
                scope="default",
                source="manual",
                version=1,
            )
            db.add(term_row)
            await db.flush()
            await svc.bump_version(db, ctx.user.id)
            current_terms += 1
            row.status = "created"
            row.term_id = term_row.id
            result_rows.append(row)
            db.add(row)
        record.state = "completed"
        from datetime import datetime, timezone

        record.completed_at = datetime.now(timezone.utc)
        await db.commit()
        state = await svc.get_state(db, ctx.user.id)
        return _import_result(import_id, result_rows, state.version, False)
