from __future__ import annotations

import asyncio
import json
import time

import httpx

from ...core.clock import loop_time
from ..asr.base import ProviderError
from .base import LlmProvider, LlmRequest, LlmResponse


class OpenAICompatibleProvider(LlmProvider):
    provider_id = "openai_compatible"

    def __init__(self, base_url: str, model_id: str, api_key: str | None, structured_output="json_object", *, thinking=None, text_probe=False):
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.api_key = api_key
        self.structured_output = structured_output
        self.thinking = thinking
        self.text_probe = text_probe
        self.client = httpx.AsyncClient(timeout=60.0, trust_env=False, follow_redirects=False)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def prepare(self) -> None:
        if not self.base_url:
            raise ProviderError("unavailable", "LLM base_url is not configured")
        try:
            response = await self.client.get(
                f"{self.base_url}/models", headers=self._headers(), timeout=5.0
            )
            response.raise_for_status()
            if self.text_probe:
                probe = LlmRequest('refine', 'svi-r2-text-health-v1 synthetic: Return only the word ready.',
                    {'transcript': 'ready', 'confirmed_terms': []},
                    {'type': 'string', 'minLength': 1, 'maxLength': 32},
                    max_output_tokens=32, output_mode='text')
                answer = await self.generate(probe, loop_time() + 5)
                if answer.error or answer.finish_reason != 'stop' or answer.text is None or answer.text.strip() != 'ready':
                    raise ProviderError('unavailable', 'synthetic text generation did not complete')
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("unavailable", str(exc)) from exc

    async def generate(self, request: LlmRequest, deadline: float) -> LlmResponse:
        started = time.monotonic()
        remaining = min(10.0, deadline - loop_time())
        if remaining <= 0:
            return LlmResponse(parsed=None, provider=self.provider_id, model_id=self.model_id,
                finish_reason="error", error=ProviderError("timeout", "deadline elapsed"))
        body = {
            "model": self.model_id,
            "temperature": 0,
            "max_tokens": request.max_output_tokens,

            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": json.dumps(request.input, ensure_ascii=False)},
            ],
        }
        if self.thinking == "disabled":
            body["thinking"] = {"type": "disabled"}
        if request.output_mode == "text":
            if request.purpose != "refine":
                return LlmResponse(None, self.provider_id, self.model_id, finish_reason="error",
                    error=ProviderError("invalid_input", "text output is only available for refine"))
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        elif self.structured_output == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": request.purpose, "schema": request.schema, "strict": True}}
        elif self.structured_output == "json_object":
            body["response_format"] = {"type": "json_object"}
        try:
            if request.output_mode == "text":
                from .text_stream import read_text_stream
                async with asyncio.timeout_at(min(deadline, loop_time() + remaining)):
                    async with self.client.stream("POST", f"{self.base_url}/chat/completions",
                            headers=self._headers(), json=body, timeout=httpx.Timeout(remaining)) as response:
                        response.raise_for_status()
                        content, usage = await read_text_stream(response)
                return LlmResponse(None, self.provider_id, self.model_id, text=content,
                    prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                    elapsed_ms=int((time.monotonic() - started) * 1000))
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
                timeout=httpx.Timeout(remaining),
            )
            response.raise_for_status()
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            finish = choice.get("finish_reason") or "error"
            content = (choice.get("message") or {}).get("content") or ""
            usage = data.get("usage") or {}
            if finish == "length":
                return LlmResponse(
                    parsed=None,
                    provider=self.provider_id,
                    model_id=self.model_id,
                    finish_reason="length",
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    error=ProviderError("truncated", "output length limit", retryable=False),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            if finish not in ("stop", "error") or (choice.get("message") or {}).get("refusal"):
                return LlmResponse(parsed=None, provider=self.provider_id, model_id=self.model_id,
                    finish_reason="refusal", prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    error=ProviderError("refused", "provider refused output"))
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                return LlmResponse(
                    parsed=None,
                    provider=self.provider_id,
                    model_id=self.model_id,
                    finish_reason="error",
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    error=ProviderError("invalid_output", str(exc), retryable=True),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            return LlmResponse(
                parsed=parsed,
                provider=self.provider_id,
                model_id=self.model_id,
                finish_reason=finish,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except ProviderError as exc:
            return LlmResponse(None, self.provider_id, self.model_id, finish_reason="error", error=exc,
                elapsed_ms=int((time.monotonic() - started) * 1000))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                error = ProviderError("rate_limited", "upstream rate limited")
                try:
                    error.retry_after_ms = max(1000, min(60000, int(float(exc.response.headers.get("Retry-After", "1")) * 1000)))
                except ValueError:
                    error.retry_after_ms = 1000
            else:
                error = ProviderError("transport_error", "upstream HTTP failure", retryable=exc.response.status_code >= 500)
            return LlmResponse(parsed=None, provider=self.provider_id, model_id=self.model_id,
                finish_reason="error", error=error, elapsed_ms=int((time.monotonic() - started) * 1000))
        except (httpx.TimeoutException, TimeoutError):
            return LlmResponse(
                parsed=None,
                provider=self.provider_id,
                model_id=self.model_id,
                finish_reason="error",
                error=ProviderError("timeout", "LLM request timed out"),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            return LlmResponse(
                parsed=None,
                provider=self.provider_id,
                model_id=self.model_id,
                finish_reason="error",
                error=ProviderError("transport_error", "LLM transport failed", retryable=True),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    async def health(self) -> bool:
        try:
            response = await self.client.get(
                f"{self.base_url}/models", headers=self._headers(), timeout=2.0
            )
            return 200 <= response.status_code < 300
        except Exception:
            return False

    async def close(self) -> None:
        await self.client.aclose()
