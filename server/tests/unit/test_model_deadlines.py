import asyncio
import time
import unittest

from app.core.clock import loop_time
from app.core.config import LlmConfig
from app.providers.asr.base import ProviderError
from app.providers.llm.base import LlmRequest, LlmResponse
from app.providers.llm.scheduled import ScheduledLlmProvider
from app.services.classifier import ClassifierService
from app.services.inference_context import CallScope, call_scope
from app.services.llm_validation import RetryAllowance, generate_validated
from app.services.refiner import RefinerService


def response(parsed, finish="stop", error=None):
    return LlmResponse(parsed=parsed, provider="stub", model_id="fixed", finish_reason=finish, error=error)


class SequenceProvider:
    provider_id, model_id = "stub", "fixed"
    def __init__(self, items):
        self.items, self.calls = list(items), []
    async def generate(self, request, deadline):
        self.calls.append((request.purpose, deadline))
        return self.items.pop(0)
    async def close(self):
        pass


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_refine_uses_one_retry_and_deadline_without_post_generation_audit(self):
        bad = response(None, error=ProviderError("invalid_output", "bad"))
        provider = SequenceProvider([bad, response({"candidate_text": "请检查这个按钮", "edits": []}), bad])
        deadline = loop_time() + 1
        result = await RefinerService(provider).refine("帮我检查一下这个按钮", deadline=deadline)
        self.assertEqual([p for p, _ in provider.calls], ["refine", "refine"])
        self.assertTrue(all(d == deadline for _, d in provider.calls))
        self.assertFalse(result.review_required)
        self.assertEqual(result.text, "请检查这个按钮")
        self.assertEqual(provider.items, [bad])

    async def test_legacy_audit_payloads_are_never_requested_or_retried(self):
        provider = SequenceProvider([
            response({"candidate_text": "请检查这个按钮", "edits": []}),
            response({"decision": "pass"}),
            response({"decision": "pass", "violations": []}),
        ])
        result = await RefinerService(provider).refine("帮我检查一下这个按钮")
        self.assertFalse(result.review_required)
        self.assertEqual([p for p, _ in provider.calls], ["refine"])
        self.assertEqual(len(provider.items), 2)

    async def test_configured_zero_retries_is_honored(self):
        provider = SequenceProvider([response({"wrong": "schema"})])
        result = await RefinerService(provider, automatic_retries=0).refine("原文")
        self.assertTrue(result.review_required)
        self.assertEqual(len(provider.calls), 1)

    async def test_classify_rejects_truncation_and_bool_string_without_learning(self):
        valid = {"decision": "term", "ambiguous": False, "category": "proper_term", "reason_code": "TERM",
                 "evidence": {"before_start": 0, "before_end": 6, "before": "re act", "after": "React"}}
        provider = SequenceProvider([response(valid, finish="length")])
        answer = await ClassifierService(provider).classify("re act", "React", "", "")
        self.assertEqual(answer.reason_code, "MODEL_BUSY")
        self.assertEqual(len(provider.calls), 1)
        malformed = dict(valid, ambiguous="false")
        provider = SequenceProvider([response(malformed), response(malformed)])
        answer = await ClassifierService(provider).classify("re act", "React", "", "")
        self.assertEqual(answer.reason_code, "MODEL_BUSY")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0][1], provider.calls[1][1])

    async def test_slow_stub_cannot_extend_absolute_deadline(self):
        class Slow:
            async def generate(self, request, deadline):
                await asyncio.sleep(1)
        request = LlmRequest("refine", "", {}, {"type": "object"})
        start = time.monotonic()
        deadline = loop_time() + .03
        with self.assertRaises(TimeoutError):
            await generate_validated(Slow(), request, deadline, RetryAllowance())
        self.assertLess(time.monotonic() - start, .3)

    async def test_expired_refine_budget_never_calls_provider(self):
        provider = SequenceProvider([])
        result = await RefinerService(provider).refine("原文", deadline=0)
        self.assertEqual(result.review_reason, "budget_exceeded")
        self.assertEqual(provider.calls, [])

    async def test_cancelled_remote_call_holds_permit_to_original_deadline(self):
        started = asyncio.Event()
        class Remote:
            provider_id, model_id = "stub", "fixed"
            async def generate(self, request, deadline):
                started.set()
                await asyncio.Future()
            async def close(self):
                pass
        provider = ScheduledLlmProvider(Remote(), LlmConfig(max_inflight=1), 2)
        deadline = loop_time() + .08
        with call_scope(CallScope("user", "session", 1)):
            task = asyncio.create_task(provider.generate(LlmRequest("refine", "", {}, {}), deadline))
        await started.wait()
        task.cancel()
        await asyncio.sleep(.01)
        self.assertEqual(provider.scheduler.snapshot()["running"], 1)
        await asyncio.gather(task, return_exceptions=True)
        self.assertGreaterEqual(loop_time(), deadline)
        self.assertEqual(provider.scheduler.snapshot()["running"], 0)
        await provider.close()
