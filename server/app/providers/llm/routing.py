"""Purpose routing keeps correction snippets away from the public R2 route."""
from __future__ import annotations

import asyncio

from ..asr.base import ProviderError
from ...core.endpoints import is_local_endpoint
from .base import LlmResponse


class CombinedSchedulerView:
    """Admin reads the sum of distinct resource pools, without hiding classify."""
    def __init__(self, *providers):
        self.providers = list({id(p): p for p in providers if p is not None}.values())

    def snapshot(self):
        values = [p.scheduler.snapshot() for p in self.providers]
        return {key: sum(v[key] for v in values) for key in ('running', 'high_waiting', 'low_waiting')}


class PurposeRoutedLlmProvider:
    def __init__(self, refine, classify=None):
        self.refine, self.classify = refine, classify
        self.provider_id, self.model_id = refine.provider_id, refine.model_id
        self.scheduler = CombinedSchedulerView(refine, classify)

    @property
    def recorder(self):
        return self.refine.recorder

    @recorder.setter
    def recorder(self, value):
        self.refine.recorder = value
        if self.classify is not None:
            self.classify.recorder = value

    async def prepare(self):
        await self.refine.prepare()
        if self.classify is not None and self.classify is not self.refine:
            try:
                async with asyncio.timeout(2):
                    await self.classify.prepare()
            except Exception:
                # Foreground availability is independent. Classification still
                # attempts its own bounded call and reports explicit failure.
                pass

    async def health(self):
        return await self.refine.health()

    async def generate(self, request, deadline):
        if request.purpose == 'refine':
            return await self.refine.generate(request, deadline)
        if request.purpose == 'classify' and self.classify is not None:
            return await self.classify.generate(request, deadline)
        return LlmResponse(None, 'unconfigured', 'unconfigured', finish_reason='error',
            error=ProviderError('unavailable', 'no authorized provider for this purpose'))

    async def close(self):
        await self.refine.close()
        if self.classify is not None and self.classify is not self.refine:
            await self.classify.close()
