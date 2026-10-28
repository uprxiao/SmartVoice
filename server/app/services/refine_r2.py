"""Fixed R2: one full text request, separate from JSON correction classification."""
from __future__ import annotations

from ..core.clock import loop_time
from ..providers.llm.base import LlmRequest
from .llm_validation import RetryAllowance, generate_validated
from .r2_dictionary import apply_aliases
from .r2_prompt import R2_SYSTEM, R2_VERSION

R2_TEXT_SCHEMA = {'type': 'string', 'minLength': 1, 'maxLength': 32768}


async def refine_r2(service, raw, terms, *, deadline=None):
    from .refiner import RefineResult, _reason_for

    deadline = min(deadline if deadline is not None else float('inf'), loop_time()+service.budget_ms/1000)
    if deadline <= loop_time():
        return RefineResult(raw, True, 'budget_exceeded')
    terms = terms or []
    confirmed = list(dict.fromkeys(t['term'] for t in terms
        if isinstance(t, dict) and isinstance(t.get('term'), str) and t['term']))
    req = LlmRequest('refine', R2_SYSTEM, {'transcript': raw, 'confirmed_terms': confirmed},
        R2_TEXT_SCHEMA, max_output_tokens=8192, system_prompt_version=R2_VERSION,
        output_schema_id='refine-r2-text-v1', output_mode='text')
    try:
        answer = await generate_validated(service.llm, req, deadline, RetryAllowance(service.automatic_retries))
    except TimeoutError:
        return RefineResult(raw, True, 'budget_exceeded')
    except Exception:
        return RefineResult(raw, True, 'llm_unavailable')
    if answer.error:
        return RefineResult(raw, True, _reason_for(answer))
    # Match the selected experimental output's outer whitespace normalization;
    # preserve every internal line break. Never feed this body back to a model.
    candidate = apply_aliases(raw, answer.text.strip(), terms)
    if loop_time() >= deadline:
        return RefineResult(raw, True, 'budget_exceeded')
    if not candidate.strip() or len(candidate) > 32768:
        return RefineResult(raw, True, 'llm_invalid_output')
    return RefineResult(candidate, False, None)
