"""Validate the finite JSON-schema subset used by all built-in model requests."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace

from ..core.clock import loop_time
from ..providers.asr.base import ProviderError
from ..providers.llm.base import LlmResponse


def matches_schema(value, schema: dict) -> bool:
    if "oneOf" in schema:
        branches = schema["oneOf"]
        if (not isinstance(branches, list) or not branches
            or not all(isinstance(branch, dict) for branch in branches)):
            return False
        # Exactly one branch, then enforce any supported sibling constraints.
        # Do not treat an unknown union keyword as an unconstrained schema.
        if sum(matches_schema(value, branch) for branch in branches) != 1:
            return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if not set(schema.get("required", ())) <= value.keys():
            return False
        if schema.get("additionalProperties") is False and not value.keys() <= properties.keys():
            return False
        return all(matches_schema(item, properties[key]) for key, item in value.items() if key in properties)
    if kind == "array":
        return (isinstance(value, list) and len(value) <= schema.get("maxItems", float("inf"))
                and all(matches_schema(item, schema.get("items", {})) for item in value))
    if kind == "string":
        return (isinstance(value, str) and schema.get("minLength", 0) <= len(value)
                <= schema.get("maxLength", float("inf")))
    if kind == "boolean":
        return type(value) is bool
    if kind == "integer":
        return (type(value) is int and schema.get("minimum", float("-inf")) <= value
                <= schema.get("maximum", float("inf")))
    if kind == "number":
        return type(value) in (int, float)
    return True


def validate_response(response: LlmResponse, schema: dict, *, output_mode="json") -> LlmResponse:
    if response.error is None and response.finish_reason != "stop":
        code = "truncated" if response.finish_reason == "length" else "refused"
        response.error = ProviderError(code, code)
    elif response.error is None:
        if output_mode == "text":
            valid = (response.parsed is None and isinstance(response.text, str)
                     and bool(response.text.strip()) and matches_schema(response.text, schema))
        elif output_mode == "json":
            valid = response.text is None and matches_schema(response.parsed, schema)
        else:
            valid = False
        if not valid:
            response.error = ProviderError("invalid_output", "LLM response does not match configured output contract", retryable=True)
            response.parsed = response.text = None
    return response


@dataclass
class RetryAllowance:
    remaining: int = 1


async def generate_validated(provider, request, deadline: float, allowance: RetryAllowance):
    canonical_schema, original_input = deepcopy(request.schema), deepcopy(request.input)
    output_mode = request.output_mode
    while True:
        dispatched = replace(request, schema=deepcopy(canonical_schema), input=deepcopy(original_input),
                             output_mode=output_mode)
        async with asyncio.timeout_at(deadline):
            response = await provider.generate(dispatched, deadline)
            if dispatched.schema != canonical_schema or dispatched.output_mode != output_mode:
                response.error = ProviderError('invalid_output', 'provider mutated the frozen request contract')
                response.parsed = response.text = None
                return response  # Contract tampering is not an ordinary format retry.
            response = validate_response(response, canonical_schema, output_mode=output_mode)
        error = response.error
        retryable = error is not None and (error.code == "invalid_output" or
                    (error.code == "transport_error" and error.retryable))
        if not retryable or allowance.remaining <= 0 or loop_time() >= deadline:
            return response
        allowance.remaining -= 1
