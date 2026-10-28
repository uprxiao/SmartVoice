from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
import uuid

from ..asr.base import ProviderError
from ...core.clock import loop_time
from ...services.inference_context import OUTCOME_WRITE_BUDGET_SECONDS, current_scope, persist_outcome
from ...services.llm_validation import validate_response
from ...services.inference_scheduler import SchedulerBusy, WeightedScheduler
from .base import LlmResponse

logger = logging.getLogger("smartvoiceinput.llm.scheduled")


class ScheduledLlmProvider:
    """Shared bounded foreground/classification pool for every actual LLM call."""
    def __init__(self, inner, config, ceiling: int):
        self.inner, self.config = inner, config
        self.provider_id, self.model_id = inner.provider_id, inner.model_id
        self.scheduler = WeightedScheduler(config.max_inflight, ceiling, config.classify_queue_size)
        self.recorder = None
        self.cooldown_until = 0.0
        self.provider_config_id = None  # admin-managed provider row (V2), None for YAML

    async def prepare(self):
        return await self.inner.prepare()

    async def health(self):
        return await self.inner.health()

    async def close(self):
        await self.scheduler.close()
        await self.inner.close()

    async def generate(self, request, deadline):
        schema, output_mode = deepcopy(request.schema), request.output_mode
        scope = current_scope.get()
        recorder = self.recorder
        reserve = OUTCOME_WRITE_BUDGET_SECONDS if recorder is not None and scope is not None else 0.0
        now = loop_time()
        if now >= deadline - reserve:
            return self._error("timeout")
        classify = request.purpose == "classify"
        # Classification is bounded per user, foreground per attempt.
        owner = ((scope.user_id, "classify") if classify and scope else
                 (scope.user_id, scope.session_id, scope.attempt_no) if scope else
                 ("unscoped", request.call_id or str(uuid.uuid4())))
        if owner in self.scheduler.running or any(owner in q for q in self.scheduler.queues.values()):
            return self._error("resource_exhausted")
        wait_ms = self.config.classify_wait_ms if classify else self.config.foreground_wait_ms
        if wait_ms == 0:
            if (len(self.scheduler.running) >= self.scheduler.slots or
                sum(len(q) for q in self.scheduler.queues.values()) + len(self.scheduler.running) >= self.scheduler.slots
                or now < self.cooldown_until):
                return self._error("resource_exhausted")
            wait_deadline = deadline
        else:
            wait_deadline = min(deadline, now + wait_ms / 1000)
        outcome = {}
        async def perform(slot):
            if loop_time() < self.cooldown_until:
                # Cooldown is consumed inside original queue/total budgets.
                remaining_wait = wait_deadline - loop_time()
                wait = self.cooldown_until - loop_time()
                if wait > remaining_wait:
                    raise SchedulerBusy("upstream cooldown")
                await asyncio.sleep(wait)
            started = loop_time()
            # Total budgets are maxima, not a promise of ten physical seconds.
            # Reserve the existing ledger allowance BEFORE dispatch so a call
            # consuming its transport budget can still commit unknown/null use.
            call_deadline = min(deadline - reserve, started + self.config.call_timeout_ms / 1000)
            if call_deadline <= started:
                return self._error("timeout")  # No dispatch, no ledger row.
            call_id = ""
            if recorder is not None and scope is not None:
                async with asyncio.timeout_at(call_deadline):
                    extra = {'provider_config_id': self.provider_config_id} if self.provider_config_id else {}
                    call_id = await recorder.started(scope, self.provider_id, self.model_id, request.purpose,
                                                     **extra)
            request.call_id = call_id
            status, error, answer = "unknown", "CLIENT_INTERRUPTED", None
            try:
                async with asyncio.timeout_at(call_deadline):
                    request.max_output_tokens = min(request.max_output_tokens, self.config.max_output_tokens)
                    answer = validate_response(await self.inner.generate(request, call_deadline), schema,
                                               output_mode=output_mode)
                for name in ("prompt_tokens", "completion_tokens"):
                    value = getattr(answer, name)
                    if type(value) is not int or value < 0:
                        setattr(answer, name, None)
                if answer.error is not None and answer.error.code == "timeout":
                    status, error = "unknown", "timeout"
                elif answer.error is None and answer.finish_reason == "stop":
                    status, error = "succeeded", None
                else:
                    status, error = "failed", answer.error.code if answer.error else answer.finish_reason
                if answer.error is not None and answer.error.code == "rate_limited":
                    delay_ms = getattr(answer.error, "retry_after_ms", 1000)
                    self.cooldown_until = max(self.cooldown_until, loop_time() + min(60000, max(1000, delay_ms)) / 1000)
                return answer
            except TimeoutError:
                # Remote execution may continue after HTTP timeout: unknown use.
                error = "timeout"
                return self._error("timeout")
            except asyncio.CancelledError:
                # An elapsed request deadline is timeout; an earlier explicit
                # cancellation keeps CLIENT_INTERRUPTED even while draining.
                if loop_time() >= call_deadline:
                    error = "timeout"
                # HTTP cancellation is not proof the remote model stopped.
                # Keep the service permit until this request's original deadline.
                await asyncio.sleep(max(0, call_deadline - loop_time()))
                raise
            finally:
                if call_id:
                    outcome.update(call_id=call_id, status=status, error_code=error,
                        elapsed_ms=int((loop_time() - started) * 1000),
                        prompt_tokens=answer.prompt_tokens if answer else None,
                        completion_tokens=answer.completion_tokens if answer else None)
        answer = None
        try:
            answer = await self.scheduler.submit(owner, "low" if classify else "high", perform,
                deadline=deadline, wait_deadline=wait_deadline)
        except SchedulerBusy:
            # Diagnostic metadata only; never log request text, parameters or
            # exception details when a queued call was not dispatched.
            purpose = request.purpose if request.purpose in {"refine", "audit", "classify"} else "unknown"
            logger.warning("LLM queue unavailable code=MODEL_BUSY purpose=%s", purpose)
            answer = self._error("resource_exhausted")
        except TimeoutError:
            answer = self._error("timeout")
        finally:
            # The scheduler has already released its real execution permit.
            recorded = await persist_outcome(recorder, outcome, deadline)
        return answer if recorded else self._error("unavailable")

    def _error(self, code):
        return LlmResponse(parsed=None, provider=self.provider_id, model_id=self.model_id,
            finish_reason="error", error=ProviderError(code, code))
