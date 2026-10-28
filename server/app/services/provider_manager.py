"""Current ASR / refine-LLM instances, their source, and hot switching (V2 §7.2–§7.4).

Single process (V1 forces one Uvicorn worker), so in-memory pointers are
the source of truth for running instances; ``provider_active`` decides what
is built at startup.

* ASR: new sessions use ``state.asr`` / ``SessionManager.asr_provider``;
  sessions already open keep their own provider session. A replaced ASR
  instance drains (no open sessions, or ``drain_timeout``) and is closed.
* LLM: activation repoints ``state.llm`` and the Refiner/Classifier
  references. A call that already started holds the old instance and
  completes there; the old instance closes once its scheduler is idle
  (no running/queued call) or after the drain timeout.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import platform
import sys

from sqlalchemy import select

from ..core.config import Config
from ..core.egress import EgressPolicy
from ..core.secrets import SecretBox, SecretError, associated_data
from ..models import ProviderActive, ProviderConfig
from ..providers import build_asr, build_bailian, build_egress, build_llm
from ..providers.asr.base import ProviderError
from ..providers.params import CLOUD_ASR_TYPES

log = logging.getLogger('svi.providers')
DRAIN_GRACE_S = 120


def local_asr_supported() -> bool:
    """`qwen3_mlx` needs a macOS Apple Silicon host (FR-11-06)."""
    return sys.platform == 'darwin' and platform.machine() == 'arm64'


@dataclasses.dataclass
class Source:
    source: str = 'yaml'           # 'db' | 'yaml' | 'none'
    reason: str | None = None      # why the DB choice was not used
    provider_id: str | None = None


class ProviderManager:
    def __init__(self, state, config: Config, *, secrets: SecretBox | None = None,
                 egress: EgressPolicy | None = None, yaml_asr=None, yaml_llm=None):
        self.state, self.config = state, config
        # YAML factories are looked up at call time so app-level overrides apply.
        self._yaml_asr = yaml_asr or build_asr
        self._yaml_llm = yaml_llm or build_llm
        try:
            self.secrets = secrets if secrets is not None else SecretBox.from_env()
            self.secrets_error = None
        except SecretError:
            self.secrets, self.secrets_error = SecretBox({}, None), 'secret_keys_invalid'
        self.egress = egress or build_egress(config)
        self.sources = {'asr': Source(), 'llm': Source()}
        self._drain_tasks: set[asyncio.Task] = set()

    # ----- building -----------------------------------------------------
    def decrypt(self, row: ProviderConfig) -> str | None:
        if row.secret_ciphertext is None:
            return None
        return self.secrets.open(row.secret_ciphertext, row.secret_key_id, associated_data(row.id, row.kind))

    def build_asr_row(self, row: ProviderConfig, api_key: str | None):
        params = dict(row.params or {})
        if row.type == 'bailian_realtime':
            params.setdefault('language', self.config.asr.language)
            provider = build_bailian(row.base_url or '', api_key, row.model_id, params, self.egress,
                                     provider_id=row.id, provider_version=str(row.version))
            provider.config = dataclasses.replace(provider.config, provider_config_id=row.id)
            return provider
        if row.type == 'qwen3_mlx':
            if not local_asr_supported():
                raise ProviderError('host_unsupported', 'local ASR requires macOS Apple Silicon')
            from ..providers.asr.qwen3_mlx import Qwen3MlxProvider
            asr = self.config.asr.model_copy(update={'provider': 'qwen3_mlx', 'model_id': row.model_id})
            provider = Qwen3MlxProvider(asr, queue_limit=self.config.effective_admission_ceiling)
        elif row.type == 'mock':
            if self.config.environment != 'development':
                raise ProviderError('unavailable', 'mock ASR is only allowed in development')
            from ..providers.asr.mock import MockAsrProvider
            provider = MockAsrProvider()
        else:
            raise ProviderError('unavailable', 'unsupported ASR provider type')
        limit = int(params.get('max_record_seconds', provider.capabilities.max_record_seconds))
        provider.capabilities = dataclasses.replace(provider.capabilities, provider_id=row.id,
                                                    provider_version=str(row.version), max_record_seconds=limit)
        return provider

    def build_llm_row(self, row: ProviderConfig, api_key: str | None):
        from ..providers import _build_llm
        llm = self.config.llm.model_copy(update={
            'provider': row.type, 'model_id': row.model_id, 'base_url': row.base_url or '',
            **{k: v for k, v in (row.params or {}).items() if k == 'max_inflight'}})
        if row.type == 'mock' and self.config.environment != 'development':
            raise ProviderError('unavailable', 'mock LLM is only allowed in development')
        inner = _build_llm(self.config, llm, api_key)
        if row.type == 'openai_compatible':
            # Same text probe as the YAML refine provider (ds_r2).
            inner.text_probe = llm.refine_protocol == 'ds_r2'
        return build_llm(self.config, inner=inner, llm_config=llm, provider_config_id=row.id)

    def build_row(self, row: ProviderConfig, api_key: str | None):
        return self.build_asr_row(row, api_key) if row.kind == 'asr' else self.build_llm_row(row, api_key)

    def yaml_asr(self):
        if self.config.asr.provider == 'mock' and self.config.environment != 'development':
            raise ProviderError('unavailable', 'mock ASR is only allowed in development')
        return self._yaml_asr(self.config)

    # ----- startup ------------------------------------------------------
    async def _active_row(self, kind: str) -> ProviderConfig | None:
        async with self.state.db.session_factory() as db:
            active = await db.get(ProviderActive, kind)
            if active is None or active.provider_id is None:
                return None
            return await db.get(ProviderConfig, active.provider_id)

    async def startup(self, kind: str):
        """Build the DB-selected provider, else fall back to YAML with a reason."""
        source = self.sources[kind]
        row = None
        try:
            row = await self._active_row(kind)
        except Exception as exc:  # noqa: BLE001
            source.reason = 'provider_table_unavailable'
            log.warning('provider_active unreadable kind=%s type=%s', kind, type(exc).__name__)
        if row is not None:
            try:
                if self.secrets_error:
                    raise SecretError(self.secrets_error)
                instance = self.build_row(row, self.decrypt(row))
                if kind == 'asr':
                    await instance.prepare()
                self.sources[kind] = Source('db', None, row.id)
                return instance
            except SecretError:
                reason = 'secret_unavailable'
            except ProviderError as exc:
                reason = exc.code if exc.code in ('host_unsupported', 'egress_denied') else 'build_failed'
            except Exception:  # noqa: BLE001
                reason = 'build_failed'
            log.warning('DB provider not used kind=%s reason=%s; falling back to YAML', kind, reason)
            self.sources[kind] = Source('yaml', reason, None)
        else:
            self.sources[kind] = Source('yaml', source.reason, None)
        try:
            instance = self.yaml_asr() if kind == 'asr' else self._yaml_llm(self.config)
        except Exception:
            self.sources[kind] = Source('none', self.sources[kind].reason or 'yaml_unavailable', None)
            raise
        return instance

    # ----- hot switch ---------------------------------------------------
    async def activate_asr(self, instance, row_id: str | None) -> None:
        state = self.state
        old = state.asr
        state.asr = instance
        instance.recorder = getattr(state, 'recorder', None)
        state.asr_prepared = True
        if getattr(state, 'sessions', None) is not None:
            state.sessions.asr_provider = instance
        self.sources['asr'] = Source('db' if row_id else 'yaml', None, row_id)
        if old is not None and old is not instance:
            self._drain(old)

    def activate_llm(self, instance, row_id: str | None) -> None:
        state = self.state
        old = state.llm
        instance.recorder = getattr(state, 'recorder', None)
        state.llm = instance
        sessions = getattr(state, 'sessions', None)
        if sessions is not None:
            sessions.refiner.llm = instance
        classifier = getattr(state, 'classifier', None)
        if classifier is not None:
            classifier.llm = instance
        state.llm_prepared = True
        self.sources['llm'] = Source('db' if row_id else 'yaml', None, row_id)
        if old is not None and old is not instance:
            self._drain(old, busy=_llm_busy, timeout=DRAIN_GRACE_S)

    def _drain(self, provider, *, busy=None, timeout: float | None = None) -> None:
        busy = busy or _open_sessions
        timeout = timeout if timeout is not None else self.config.audio.max_duration_seconds + DRAIN_GRACE_S

        async def drain():
            loop = asyncio.get_running_loop()
            ends = loop.time() + timeout
            while loop.time() < ends and busy(provider):
                await asyncio.sleep(0.2)
            try:
                await provider.close()
            except Exception as exc:  # noqa: BLE001
                log.warning('ASR provider close failed type=%s', type(exc).__name__)

        task = asyncio.get_running_loop().create_task(drain())
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)

    async def close(self) -> None:
        for task in list(self._drain_tasks):
            task.cancel()
        await asyncio.gather(*self._drain_tasks, return_exceptions=True)

    def limits(self) -> dict:
        caps = getattr(self.state.asr, 'capabilities', None)
        sessions = getattr(self.state, 'sessions', None)
        return {
            'max_record_seconds': sessions._limit_seconds() if sessions else None,
            'max_concurrent_sessions': getattr(caps, 'max_concurrent_sessions', None),
            'is_cloud': getattr(caps, 'replay_rate', None) is not None,
        }


def _llm_busy(provider) -> int:
    scheduler = getattr(provider, 'scheduler', None)
    if scheduler is None:
        return 0
    snapshot = scheduler.snapshot()
    return snapshot['running'] + snapshot['high_waiting'] + snapshot['low_waiting']


def _open_sessions(provider) -> int:
    sessions = getattr(provider, 'sessions', None)
    if sessions is None:
        engine = getattr(provider, 'engine', None)
        sessions = getattr(engine, 'sessions', None)
    return len(sessions) if sessions is not None else 0


__all__ = ['ProviderManager', 'Source', 'local_asr_supported', 'CLOUD_ASR_TYPES']
