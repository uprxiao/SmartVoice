from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import sys
import warnings

from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import case, func, null, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from .core.config import load_config
from .core.db import Database
from .core.security import hash_password, normalize_email
from .models import (
    DictionaryState, LearningEvent, LoginSession, SessionAttempt, SessionResult,
    User, VoiceSession,
)


class SafeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        # The retired --password option may be supplied accidentally. Do not
        # echo unrecognized arguments, which could include a plaintext secret.
        self.exit(2, "参数无效；请用 --help 查看用法。密码仅接受隐藏输入或 --password-stdin。\n")


class MaintenanceError(ValueError):
    """A fixed public error code; never wrap exception text or input values."""


def _email(value: str) -> str:
    value = normalize_email(value)
    if len(value) > 254 or "@" not in value or value.startswith("@"):
        raise MaintenanceError("INVALID_EMAIL")
    return value


def _password(value: str) -> str:
    if not 8 <= len(value) <= 128:
        raise MaintenanceError("INVALID_PASSWORD_LENGTH")
    return value


def _audit(operation: str, outcome: str, **metadata) -> dict:
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "stage": "maintenance",
            "operation": operation, "outcome": outcome, **metadata}


async def _schema_head(db) -> None:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    expected = set(ScriptDirectory.from_config(cfg).get_heads())
    connection = await db.connection()
    actual = set(await connection.run_sync(
        lambda connection: MigrationContext.configure(connection).get_current_heads()
    ))
    if not expected or actual != expected:
        raise MaintenanceError("SCHEMA_REVISION_MISMATCH")


async def create_admin(email: str, password: str, display_name: str | None = None) -> dict:
    normalized = _email(email)
    _password(password)
    if display_name is not None and len(display_name) > 64:
        raise MaintenanceError("INVALID_DISPLAY_NAME")
    config = load_config()
    # One operation per maintenance process; no DB connection/transaction while hashing.
    encoded = await asyncio.to_thread(hash_password, config.auth, password)
    database = Database(config)
    try:
        async with database.session_factory() as session:
            try:
                async with session.begin():
                    await _schema_head(session)
                    # Serialize bootstrap operations, including distinct email addresses.
                    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('svi.admin.init', 0))"))
                    existing = await session.scalar(select(User).where(User.email == normalized).with_for_update())
                    if existing is not None:
                        if existing.role != "admin":
                            raise MaintenanceError("EMAIL_EXISTS_NOT_ADMIN")
                        # Preserve password, role, disabled status, display name, and settings.
                        return _audit("admin.init", "already_exists", user_id=existing.id)
                    if await session.scalar(select(User.id).where(User.role == "admin").limit(1)):
                        raise MaintenanceError("ADMIN_ALREADY_INITIALIZED")
                    user = User(email=normalized, password_hash=encoded, role="admin", status="active",
                                display_name=display_name)
                    session.add(user)
                    await session.flush()
                    session.add(DictionaryState(user_id=user.id))
                    user_id = user.id
            except IntegrityError:
                # Registration can race bootstrap outside its advisory lock.
                await session.rollback()
                if await session.scalar(select(User.id).where(User.email == normalized)):
                    raise MaintenanceError("EMAIL_EXISTS") from None
                raise MaintenanceError("DATABASE_CONSTRAINT_ERROR") from None
        return _audit("admin.init", "created", user_id=user_id)
    finally:
        await database.close()


async def reset_password(email: str, password: str) -> dict:
    normalized = _email(email)
    _password(password)
    config = load_config()
    encoded = await asyncio.to_thread(hash_password, config.auth, password)
    database = Database(config)
    try:
        async with database.session_factory() as session:
            async with session.begin():
                await _schema_head(session)
                user = await session.scalar(select(User).where(User.email == normalized).with_for_update())
                if user is None:
                    raise MaintenanceError("USER_NOT_FOUND")
                # Same User -> LoginSession order as login/refresh/disable.
                logins = list((await session.scalars(select(LoginSession).where(
                    LoginSession.user_id == user.id).order_by(LoginSession.id).with_for_update())).all())
                now = datetime.now(timezone.utc)
                revoked = 0
                for login in logins:
                    if login.revoked_at is None:
                        login.revoked_at = now
                        revoked += 1
                user.password_hash = encoded
                user_id = user.id
        return _audit("account.reset_password", "updated", user_id=user_id, revoked_sessions=revoked)
    finally:
        await database.close()


async def restore_sanitize(confirm_database: str) -> dict:
    """Offline post-restore barrier: unconditional plaintext purge and sid revocation."""
    config = load_config()
    if not confirm_database or make_url(config.database_url).database != confirm_database:
        raise MaintenanceError("DATABASE_CONFIRMATION_MISMATCH")
    database = Database(config)
    try:
        async with database.session_factory() as session:
            async with session.begin():
                await _schema_head(session)
                acquired = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                                {"key": config.server.instance_lock_key})
                if not acquired:
                    raise MaintenanceError("SERVER_MUST_BE_OFFLINE")
                # Prevent concurrent password recovery from inverting User -> sid order.
                list((await session.scalars(select(User.id).order_by(User.id).with_for_update())).all())
                now = datetime.now(timezone.utc)
                sessions = await session.execute(update(VoiceSession).values(
                    content_cleared_at=func.coalesce(VoiceSession.content_cleared_at, now),
                    asr_hints=null(), llm_hints=null()))
                attempts = await session.execute(update(SessionAttempt).values(raw_text=None))
                results = await session.execute(update(SessionResult).values(
                    content_cleared_at=func.coalesce(SessionResult.content_cleared_at, now),
                    raw_text=None, refined_text=None))
                pending = LearningEvent.status.in_(("pending", "suggested"))
                events = await session.execute(update(LearningEvent).values(
                    content_cleared_at=func.coalesce(LearningEvent.content_cleared_at, now),
                    before_text=None, after_text=None, left_context=None, right_context=None,
                    evidence=null(), term_before=null(),
                    reason=case((pending, "CONTENT_EXPIRED"), else_=LearningEvent.reason),
                    status=case((pending, "failed"), else_=LearningEvent.status)))
                revoked = await session.execute(update(LoginSession).where(LoginSession.revoked_at.is_(None))
                                                .values(revoked_at=now))
                counts = {"sessions": sessions.rowcount, "attempts": attempts.rowcount,
                          "results": results.rowcount, "learning_events": events.rowcount,
                          "revoked_sessions": revoked.rowcount}
        return _audit("restore.sanitize", "completed", **counts)
    finally:
        await database.close()


async def rotate_secrets(to_key_id: str, *, box=None, database=None, _fail_on: int | None = None) -> dict:
    """Re-encrypt every provider key with ``to_key_id`` in ONE transaction.

    Each new blob is decrypted again before commit; any failure rolls back so
    all rows stay readable with their previous key (V2 §13 T7).
    ``_fail_on`` is a test-only fault injection (1-based row number).
    """
    from .core.secrets import SecretBox, SecretError, associated_data
    from .models import ProviderConfig
    box = box or SecretBox.from_env()
    if to_key_id not in box.key_ids():
        raise MaintenanceError("SECRET_KEY_NOT_CONFIGURED")
    owned = database is None
    database = database or Database(load_config())
    try:
        async with database.session_factory() as session:
            async with session.begin():
                await _schema_head(session)
                rows = list((await session.scalars(select(ProviderConfig).where(
                    ProviderConfig.secret_ciphertext.is_not(None)).order_by(ProviderConfig.id)
                    .with_for_update())).all())
                changed = 0
                for number, row in enumerate(rows, start=1):
                    aad = associated_data(row.id, row.kind)
                    try:
                        plaintext = box.open(row.secret_ciphertext, row.secret_key_id, aad)
                        if row.secret_key_id != to_key_id:
                            sealed = box.seal(plaintext, aad, to_key_id)
                            if _fail_on == number:
                                raise SecretError("injected rotation failure")
                            if box.open(sealed.ciphertext, sealed.key_id, aad) != plaintext:
                                raise SecretError("re-encryption verification failed")
                            row.secret_ciphertext, row.secret_key_id = sealed.ciphertext, sealed.key_id
                            changed += 1
                    except SecretError as exc:
                        raise MaintenanceError("SECRET_ROTATION_FAILED") from exc
                    finally:
                        plaintext = None
        return _audit("secrets.rotate", "rotated", key_id=to_key_id, rows=len(rows), changed=changed)
    finally:
        if owned:
            await database.close()


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        if sys.stdin.isatty():
            raise MaintenanceError("PASSWORD_STDIN_MUST_BE_PIPE")
        # Limit bytes/characters before validation; reject extra lines instead of
        # accidentally treating a file's first line as the intended secret.
        value = sys.stdin.read(131)
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        if "\n" in value or "\r" in value:
            raise MaintenanceError("PASSWORD_STDIN_MUST_BE_ONE_LINE")
        return _password(value)
    if not sys.stdin.isatty():
        raise MaintenanceError("USE_PASSWORD_STDIN_FOR_PIPE")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return _password(getpass.getpass("密码: "))


def _password_arguments(parser):
    parser.add_argument("--email", required=True)
    parser.add_argument("--password-stdin", action="store_true", help="从受限管道读取一行密码；参数列表不带密码")


def main() -> int:
    parser = SafeArgumentParser(prog="svi", description="SmartVoiceInput server maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("admin")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    init = admin_sub.add_parser("init", help="幂等初始化首位管理员；不重置/提权已有账号")
    _password_arguments(init)
    init.add_argument("--display-name")
    legacy = sub.add_parser("create-admin", help="admin init 的兼容名称；不再重置已有账号")
    _password_arguments(legacy)
    legacy.add_argument("--display-name")
    account = sub.add_parser("account")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    recovery = account_sub.add_parser("reset-password", help="显式更新目标密码并撤销全部登录会话")
    _password_arguments(recovery)
    restore = sub.add_parser("restore-sanitize", help="恢复开放前离线清理全部历史正文和登录会话")
    restore.add_argument("--confirm-database", required=True, help="显式确认配置指向的恢复数据库名")
    secrets_cmd = sub.add_parser("secrets", help="provider 密钥主密钥维护（主密钥只来自环境变量）")
    secrets_sub = secrets_cmd.add_subparsers(dest="secrets_command", required=True)
    rotate = secrets_sub.add_parser("rotate", help="用指定主密钥重新加密全部 provider Key（单事务，失败全部回滚）")
    rotate.add_argument("--to", required=True, dest="to_key_id", help="目标主密钥 id（须已在 SVI_SECRET_KEYS 中）")
    secrets_sub.add_parser("generate-key", help="生成一个新的 32 字节主密钥（base64），只打印一次")
    args = parser.parse_args()
    try:
        if args.command in ("admin", "create-admin"):
            result = asyncio.run(create_admin(args.email, _read_password(args.password_stdin), args.display_name))
        elif args.command == "account":
            result = asyncio.run(reset_password(args.email, _read_password(args.password_stdin)))
        elif args.command == "secrets":
            if args.secrets_command == "generate-key":
                from .core.secrets import generate_key
                print(generate_key())
                return 0
            result = asyncio.run(rotate_secrets(args.to_key_id))
        else:
            result = asyncio.run(restore_sanitize(args.confirm_database))
    except MaintenanceError as exc:
        print(json.dumps(_audit("maintenance", "failed", error_code=exc.args[0])))
        return 1
    except Exception:
        # Connection/config errors can contain secrets; never print raw exception text.
        print(json.dumps(_audit("maintenance", "failed", error_code="MAINTENANCE_FAILED")))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
