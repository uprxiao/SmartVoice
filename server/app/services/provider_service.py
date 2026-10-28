"""Admin provider configuration: validate → test → CAS write → audit (V2 §6–§8).

Error codes follow design §8.4 exactly. Responses, audit rows and logs never
contain the API key, a full workspace URL or raw provider payloads.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..api.deps import ApiError
from ..core.clock import loop_time
from ..core.egress import EgressDenied, redact_host
from ..core.secrets import SecretError, associated_data
from ..models import ProviderActive, ProviderAudit, ProviderConfig, new_uuid
from ..providers.asr.base import AsrOpenOptions, ProviderError
from ..providers.params import CLOUD_ASR_TYPES, params_model
from .provider_manager import ProviderManager, local_asr_supported

log = logging.getLogger('svi.providers')
TEST_TIMEOUT_S = 15.0
TEST_REASONS = frozenset({'auth_failed', 'rate_limited', 'unavailable', 'timeout', 'bad_request'})
NEEDS_URL = frozenset({'bailian_realtime', 'openai_compatible', 'ollama'})
NEEDS_KEY = frozenset({'bailian_realtime'})
EDITABLE = ('name', 'base_url', 'model_id', 'params', 'enabled')
CONNECTION_FIELDS = frozenset({'base_url', 'model_id', 'params', 'api_key'})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def invalid(fields: list[str], reason: str | None = None, message: str = '参数不合法') -> ApiError:
    details = {'fields': fields}
    if reason:
        details['reason'] = reason
    return ApiError(422, 'VALIDATION_ERROR', message, details=details)


def test_failed(reason: str) -> ApiError:
    return ApiError(502, 'PROVIDER_TEST_FAILED', '连接测试失败', details={'reason': reason})


class TestFailed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason if reason in TEST_REASONS else 'unavailable'


def _http_reason(exc: BaseException) -> str | None:
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return ('auth_failed' if status in (401, 403) else 'rate_limited' if status == 429
                    else 'bad_request' if 400 <= status < 500 else 'unavailable')
        if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
            return 'timeout'
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return None


class ProviderService:
    def __init__(self, state, manager: ProviderManager):
        self.state, self.manager = state, manager
        self.config = manager.config

    # ----- validation ---------------------------------------------------
    def _validate(self, kind: str, data: dict) -> dict:
        if kind not in ('asr', 'llm'):
            raise invalid(['kind'])
        provider_type = data.get('type')
        try:
            model = params_model(kind, provider_type)
        except ValueError:
            raise invalid(['type']) from None
        if kind == 'asr' and provider_type == 'qwen3_mlx' and not local_asr_supported():
            raise invalid(['type'], 'host_unsupported', '当前主机不支持本机识别')
        if provider_type == 'mock' and self.config.environment != 'development':
            raise invalid(['type'])
        name = data.get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise invalid(['name'])
        model_id = data.get('model_id')
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 128:
            raise invalid(['model_id'])
        params = data.get('params') or {}
        if not isinstance(params, dict):
            raise invalid(['params'])
        try:
            params = model.model_validate(params).model_dump(exclude_unset=True)
        except ValidationError as exc:
            fields = sorted({str(err['loc'][0]) for err in exc.errors() if err.get('loc')})
            raise invalid(fields or ['params']) from None
        base_url = data.get('base_url')
        if provider_type in NEEDS_URL:
            if not isinstance(base_url, str) or not base_url or len(base_url) > 512:
                raise invalid(['base_url'])
            try:
                self.manager.egress.check_url(base_url, kind=kind)
            except EgressDenied:
                raise invalid(['base_url'], 'egress_denied', '地址不在出口白名单内') from None
        else:
            base_url = None
        return {'type': provider_type, 'name': name.strip(), 'model_id': model_id.strip(),
                'params': params, 'base_url': base_url}

    async def _resolve(self, kind: str, base_url: str | None) -> None:
        if not base_url:
            return
        try:
            await self.manager.egress.resolve(self.manager.egress.check_url(base_url, kind=kind))
        except EgressDenied:
            raise invalid(['base_url'], 'egress_denied', '地址不在出口白名单内') from None

    def _key_required(self, provider_type: str, api_key: str | None) -> None:
        if provider_type in NEEDS_KEY and not api_key:
            raise invalid(['api_key'])

    # ----- connection test ---------------------------------------------
    async def _probe(self, row: ProviderConfig, api_key: str | None, *, keep: bool = False):
        """Build an instance and test it; returns the instance when ``keep``."""
        try:
            instance = self.manager.build_row(row, api_key)
        except ProviderError as exc:
            if exc.code == 'host_unsupported':
                raise invalid(['type'], 'host_unsupported', '当前主机不支持本机识别') from None
            raise TestFailed('unavailable') from exc
        except Exception as exc:  # noqa: BLE001
            raise TestFailed('unavailable') from exc
        try:
            if row.kind == 'asr':
                await self._probe_asr(row, instance)
            else:
                await self._probe_llm(instance)
        except BaseException:
            await _quiet_close(instance)
            raise
        if keep:
            return instance
        await _quiet_close(instance)
        return None

    async def _probe_asr(self, row: ProviderConfig, instance) -> None:
        if row.type == 'qwen3_mlx':
            # Host support was checked; the model itself loads on activation.
            return
        if row.type not in CLOUD_ASR_TYPES:
            return
        session = None
        # A connection test reports the first failure; no internal reconnects.
        # Only for the probe: an instance kept for activation must get its real
        # reconnect budget back (real-cloud finding 2026-10-28).
        original = instance.config
        instance.config = dataclasses.replace(original, max_reconnects=0)
        try:
            # 1 s of synthetic silence, commit, wait for completed/error (§7.1).
            session = await instance.open(AsrOpenOptions(session_id='provider-test', attempt_no=1,
                                                         max_samples=2 * 16000))
            await session.feed(b'\0\0' * 16000, 0)
            await session.finish(16000, loop_time() + TEST_TIMEOUT_S)
        except ProviderError as exc:
            if exc.code == 'egress_denied':
                raise invalid(['base_url'], 'egress_denied', '地址不在出口白名单内') from None
            raise TestFailed('rate_limited' if exc.code == 'busy' else exc.code) from None
        finally:
            if session is not None:
                await session.cancel()
            instance.config = original

    async def _probe_llm(self, instance) -> None:
        try:
            async with asyncio.timeout(TEST_TIMEOUT_S):
                await instance.prepare()
        except TimeoutError:
            raise TestFailed('timeout') from None
        except ProviderError as exc:
            raise TestFailed(_http_reason(exc) or exc.code) from None
        except Exception as exc:  # noqa: BLE001
            raise TestFailed(_http_reason(exc) or 'unavailable') from None

    # ----- persistence helpers -----------------------------------------
    async def _audit(self, db, actor: str, action: str, kind: str, provider_id: str | None,
                     fields: list[str], result: str, reason: str | None = None) -> None:
        db.add(ProviderAudit(actor_user_id=actor, action=action, kind=kind, provider_id=provider_id,
                             changed_fields=sorted(fields), result=result, reason=reason))

    async def _audit_now(self, *args, **kwargs) -> None:
        async with self.state.db.session_factory() as db:
            await self._audit(db, *args, **kwargs)
            await db.commit()

    async def _active(self, db, kind: str, *, lock: bool = False) -> ProviderActive:
        query = select(ProviderActive).where(ProviderActive.kind == kind)
        if lock:
            query = query.with_for_update()
        row = await db.scalar(query)
        if row is None:
            # Seeded by migration; recreated if an operator truncated it.
            db.add(ProviderActive(kind=kind, provider_id=None, version=1, updated_at=utcnow()))
            await db.flush()
            row = await db.scalar(query)
        return row

    def _seal(self, row: ProviderConfig, api_key: str | None) -> None:
        if api_key:
            if not self.manager.secrets.available:
                raise ApiError(503, 'SERVICE_UNAVAILABLE', '服务端未配置密钥加密主密钥',
                               details={'reason': 'secret_unavailable'})
            sealed = self.manager.secrets.seal(api_key, associated_data(row.id, row.kind))
            row.secret_ciphertext, row.secret_key_id, row.secret_last4 = (
                sealed.ciphertext, sealed.key_id, sealed.last4)
        else:
            row.secret_ciphertext = row.secret_key_id = row.secret_last4 = None

    def _decrypt(self, row: ProviderConfig) -> str | None:
        try:
            return self.manager.decrypt(row)
        except SecretError:
            raise ApiError(503, 'SERVICE_UNAVAILABLE', '无法解密已保存的密钥',
                           details={'reason': 'secret_unavailable'}) from None

    @staticmethod
    def _record_test(row: ProviderConfig, reason: str | None) -> None:
        row.last_test_at, row.last_test_ok, row.last_test_reason = utcnow(), reason is None, reason

    # ----- views --------------------------------------------------------
    def view(self, row: ProviderConfig, active_ids: set[str]) -> dict:
        return {
            'id': row.id, 'kind': row.kind, 'name': row.name, 'type': row.type,
            'base_url': redact_host(row.base_url) if row.base_url else None,
            'model_id': row.model_id, 'params': row.params or {}, 'enabled': row.enabled,
            'is_active': row.id in active_ids, 'is_cloud': row.type in CLOUD_ASR_TYPES,
            'secret_set': row.secret_ciphertext is not None, 'secret_last4': row.secret_last4,
            'last_test_at': row.last_test_at, 'last_test_ok': row.last_test_ok,
            'last_test_reason': row.last_test_reason, 'version': row.version,
            'updated_at': row.updated_at,
        }

    async def _active_ids(self, db) -> set[str]:
        return {pid for pid in (await db.scalars(select(ProviderActive.provider_id))).all() if pid}

    async def list(self, kind: str | None) -> list[dict]:
        async with self.state.db.session_factory() as db:
            query = select(ProviderConfig).order_by(ProviderConfig.kind, ProviderConfig.name)
            if kind:
                if kind not in ('asr', 'llm'):
                    raise invalid(['kind'])
                query = query.where(ProviderConfig.kind == kind)
            rows = (await db.scalars(query)).all()
            active = await self._active_ids(db)
            return [self.view(row, active) for row in rows]

    async def active(self) -> dict:
        out = {}
        async with self.state.db.session_factory() as db:
            active_ids = await self._active_ids(db)
            for kind in ('asr', 'llm'):
                row = await self._active(db, kind)
                config = await db.get(ProviderConfig, row.provider_id) if row.provider_id else None
                source = self.manager.sources[kind]
                out[kind] = {
                    'provider': self.view(config, active_ids) if config else None,
                    'source': source.source, 'source_reason': source.reason,
                    'running_provider_id': source.provider_id,
                    'version': row.version, 'confirmed_cloud_at': row.confirmed_cloud_at,
                }
            await db.commit()
        limits = self.manager.limits()
        out['asr'].update(max_record_seconds=limits['max_record_seconds'],
                          max_concurrent_sessions=limits['max_concurrent_sessions'])
        out['host'] = {'local_asr_supported': local_asr_supported(),
                       'egress_policy': self.config.security.egress_policy,
                       'egress_allowlist': list(self.config.security.egress_allowlist),
                       'environment': self.config.environment}
        return out

    async def audit(self, limit: int, after_seq: int | None) -> tuple[list[dict], int | None]:
        async with self.state.db.session_factory() as db:
            query = select(ProviderAudit).order_by(ProviderAudit.seq.desc()).limit(limit + 1)
            if after_seq is not None:
                query = query.where(ProviderAudit.seq < after_seq)
            rows = (await db.scalars(query)).all()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [{'id': r.id, 'at': r.at, 'actor_user_id': r.actor_user_id, 'action': r.action,
                  'kind': r.kind, 'provider_id': r.provider_id, 'changed_fields': r.changed_fields,
                  'result': r.result, 'reason': r.reason} for r in rows]
        return items, (rows[-1].seq if more and rows else None)

    # ----- mutations ----------------------------------------------------
    async def create(self, actor: str, data: dict) -> dict:
        kind = data.get('kind')
        clean = self._validate(kind, data)
        api_key = data.get('api_key') or None
        self._key_required(clean['type'], api_key)
        async with self.state.db.session_factory() as db:
            exists = await db.scalar(select(func.count()).select_from(ProviderConfig).where(
                ProviderConfig.kind == kind, ProviderConfig.name == clean['name']))
        if exists:
            raise ApiError(409, 'PROVIDER_NAME_EXISTS', '同类配置名称已存在', details={'fields': ['name']})
        await self._resolve(kind, clean['base_url'])
        row = ProviderConfig(id=new_uuid(), kind=kind, enabled=bool(data.get('enabled', True)), version=1,
                             updated_by=actor, **clean)
        try:
            await self._probe(row, api_key)
        except TestFailed as exc:
            await self._audit_now(actor, 'create', kind, None, _field_names(data), 'failed', exc.reason)
            raise test_failed(exc.reason) from None
        self._seal(row, api_key)
        self._record_test(row, None)
        async with self.state.db.session_factory() as db:
            db.add(row)
            await self._audit(db, actor, 'create', kind, row.id, _field_names(data), 'ok')
            try:
                await db.commit()
            except IntegrityError:
                raise ApiError(409, 'PROVIDER_NAME_EXISTS', '同类配置名称已存在',
                               details={'fields': ['name']}) from None
            active = await self._active_ids(db)
            return self.view(row, active)

    async def _get(self, db, provider_id: str, *, lock: bool = False) -> ProviderConfig:
        query = select(ProviderConfig).where(ProviderConfig.id == provider_id)
        if lock:
            query = query.with_for_update()
        row = await db.scalar(query)
        if row is None:
            raise ApiError(404, 'NOT_FOUND', '配置不存在')
        return row

    async def update(self, actor: str, provider_id: str, data: dict) -> dict:
        version = data.get('version')
        if not isinstance(version, int) or isinstance(version, bool):
            raise invalid(['version'])
        async with self.state.db.session_factory() as db:
            row = await self._get(db, provider_id)
            active = await self._active_ids(db)
            db.expunge(row)
        if row.version != version:
            raise ApiError(409, 'VERSION_CONFLICT', '配置已被修改，请刷新后重试',
                           details={'current_version': row.version})
        merged = {'type': row.type, **{f: getattr(row, f) for f in EDITABLE}}
        changed = [f for f in EDITABLE if f in data and data[f] != getattr(row, f)]
        merged.update({f: data[f] for f in changed})
        key_change = 'api_key' in data
        if key_change:
            changed.append('api_key')
        if row.id in active and merged['enabled'] is False:
            raise ApiError(409, 'PROVIDER_ACTIVE', '当前使用中的配置不能停用')
        clean = self._validate(row.kind, merged)
        api_key = (data.get('api_key') or None) if key_change else self._decrypt(row)
        self._key_required(clean['type'], api_key)
        candidate = ProviderConfig(id=row.id, kind=row.kind, enabled=merged['enabled'],
                                   version=row.version + 1, **clean)
        swap = None
        if CONNECTION_FIELDS & set(changed):
            await self._resolve(row.kind, clean['base_url'])
            try:
                swap = await self._probe(candidate, api_key, keep=row.id in active)
                if swap is not None and row.kind == 'asr':
                    await swap.prepare()
            except ProviderError as exc:
                await _quiet_close(swap)
                await self._audit_now(actor, 'update', row.kind, row.id, changed, 'failed', 'unavailable')
                raise test_failed('unavailable') from exc
            except TestFailed as exc:
                await self._audit_now(actor, 'update', row.kind, row.id, changed, 'failed', exc.reason)
                raise test_failed(exc.reason) from None
        async with self.state.db.session_factory() as db:
            locked = await self._get(db, provider_id, lock=True)
            if locked.version != version:
                await _quiet_close(swap)
                raise ApiError(409, 'VERSION_CONFLICT', '配置已被修改，请刷新后重试',
                               details={'current_version': locked.version})
            for field, value in clean.items():
                setattr(locked, field, value)
            locked.enabled = merged['enabled']
            if key_change:
                self._seal(locked, api_key)
            if CONNECTION_FIELDS & set(changed):
                self._record_test(locked, None)
            locked.version += 1
            locked.updated_at, locked.updated_by = utcnow(), actor
            await self._audit(db, actor, 'update', locked.kind, locked.id, changed, 'ok')
            try:
                await db.commit()
            except IntegrityError:
                await _quiet_close(swap)
                raise ApiError(409, 'PROVIDER_NAME_EXISTS', '同类配置名称已存在',
                               details={'fields': ['name']}) from None
            view = self.view(locked, await self._active_ids(db))
        if swap is not None:
            await self._publish(locked.kind, swap, locked.id)
        return view

    async def delete(self, actor: str, provider_id: str) -> None:
        async with self.state.db.session_factory() as db:
            row = await self._get(db, provider_id, lock=True)
            if row.id in await self._active_ids(db):
                raise ApiError(409, 'PROVIDER_ACTIVE', '当前使用中的配置不能删除')
            kind = row.kind
            # Clear the ciphertext first, then remove the row (§7.1).
            row.secret_ciphertext = row.secret_key_id = row.secret_last4 = None
            await db.flush()
            await db.delete(row)
            await self._audit(db, actor, 'delete', kind, provider_id, [], 'ok')
            await db.commit()

    async def test(self, actor: str, provider_id: str) -> dict:
        async with self.state.db.session_factory() as db:
            row = await self._get(db, provider_id)
            db.expunge(row)
        api_key = self._decrypt(row)
        reason = None
        try:
            await self._resolve(row.kind, row.base_url)
            await self._probe(row, api_key)
        except TestFailed as exc:
            reason = exc.reason
        async with self.state.db.session_factory() as db:
            locked = await self._get(db, provider_id, lock=True)
            self._record_test(locked, reason)  # version and other fields untouched
            await self._audit(db, actor, 'test', locked.kind, locked.id, [], 'failed' if reason else 'ok', reason)
            await db.commit()
            view = self.view(locked, await self._active_ids(db))
        if reason:
            raise test_failed(reason)
        return view

    async def activate(self, actor: str, data: dict) -> dict:
        kind, provider_id, version = data.get('kind'), data.get('provider_id'), data.get('version')
        if kind not in ('asr', 'llm'):
            raise invalid(['kind'])
        if not isinstance(provider_id, str):
            raise invalid(['provider_id'])
        if not isinstance(version, int) or isinstance(version, bool):
            raise invalid(['version'])
        async with self.state.db.session_factory() as db:
            row = await self._get(db, provider_id)
            current = await self._active(db, kind)
            db.expunge(row)
            confirmed = current.confirmed_cloud_at is not None
            current_version = current.version
            await db.commit()
        if row.kind != kind:
            raise invalid(['provider_id'])
        if not row.enabled:
            raise ApiError(409, 'PROVIDER_DISABLED', '已停用的配置不能设为当前')
        cloud = kind == 'asr' and row.type in CLOUD_ASR_TYPES
        if cloud and not confirmed and data.get('confirm_cloud') is not True:
            raise invalid(['confirm_cloud'], 'cloud_confirmation_required', '启用云端识别需要确认隐私说明')
        if current_version != version:
            raise ApiError(409, 'VERSION_CONFLICT', '当前配置已被修改，请刷新后重试',
                           details={'current_version': current_version})
        api_key = self._decrypt(row)
        try:
            await self._resolve(kind, row.base_url)
            instance = await self._probe(row, api_key, keep=True)
            if kind == 'asr':
                await instance.prepare()
        except TestFailed as exc:
            await self._audit_now(actor, 'activate', kind, row.id, ['provider_id'], 'failed', exc.reason)
            raise test_failed(exc.reason) from None
        except ProviderError as exc:
            await self._audit_now(actor, 'activate', kind, row.id, ['provider_id'], 'failed', 'unavailable')
            raise test_failed('unavailable') from exc
        try:
            async with self.state.db.session_factory() as db:
                locked = await self._active(db, kind, lock=True)
                if locked.version != version:
                    raise ApiError(409, 'VERSION_CONFLICT', '当前配置已被修改，请刷新后重试',
                                   details={'current_version': locked.version})
                if cloud and locked.confirmed_cloud_at is None:
                    locked.confirmed_cloud_at, locked.confirmed_cloud_by = utcnow(), actor
                    await self._audit(db, actor, 'confirm_cloud', kind, row.id, ['confirm_cloud'], 'ok')
                locked.provider_id = row.id
                locked.version += 1
                locked.updated_at, locked.updated_by = utcnow(), actor
                await self._audit(db, actor, 'activate', kind, row.id, ['provider_id'], 'ok')
                await db.commit()
        except BaseException:
            await _quiet_close(instance)
            raise
        await self._publish(kind, instance, row.id)
        return await self.active()

    async def _publish(self, kind: str, instance, row_id: str) -> None:
        if kind == 'asr':
            await self.manager.activate_asr(instance, row_id)
        else:
            self.manager.activate_llm(instance, row_id)
        log.info('provider activated kind=%s provider=%s', kind, row_id)


def _field_names(data: dict) -> list[str]:
    return [k for k in data if k not in ('kind',)]


async def _quiet_close(instance) -> None:
    if instance is None:
        return
    try:
        await instance.close()
    except Exception:  # noqa: BLE001
        pass
