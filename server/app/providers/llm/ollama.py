from __future__ import annotations

import asyncio
import json
import time

import httpx

from ...core.clock import loop_time
from ..asr.base import ProviderError
from .base import LlmProvider, LlmRequest, LlmResponse


class OllamaProvider(LlmProvider):
    provider_id = "ollama"

    def __init__(self, base_url: str, model_id: str, context_tokens: int = 8_192):
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.context_tokens = context_tokens
        self.client = httpx.AsyncClient(
            base_url=self.base_url, timeout=60.0, trust_env=False, follow_redirects=False
        )
        self.supports_thinking = False

    async def prepare(self) -> None:
        try:
            response = await self.client.get("/api/tags")
            response.raise_for_status()
            models = response.json().get("models", [])
            model = next((m for m in models if m.get("name") == self.model_id), None)
            if not model or model.get("remote_host") or model.get("remote_model"):
                raise ProviderError(
                    "unavailable", "configured model is not installed locally"
                )
            detail = await self.client.post("/api/show", json={"model": self.model_id})
            detail.raise_for_status()
            data = detail.json()
            if data.get("remote_host") or data.get("remote_model"):
                raise ProviderError("unavailable", "cloud models are not allowed")
            self.supports_thinking = "thinking" in data.get("capabilities", [])
            # A prompt-free generate only loads weights; HTTP200 doesn't prove
            # the model can actually generate. Never use user text to warm it.
            probe = {
                "model": self.model_id,
                "prompt": 'svi-llm-health-v1 synthetic: Return only JSON {"ready":true}.',
                "format": {"type": "object", "properties": {"ready": {"type": "boolean"}},
                           "required": ["ready"], "additionalProperties": False},
                "keep_alive": -1,
                "options": {"num_ctx": self.context_tokens, "temperature": 0, "num_predict": 32},
                "stream": False,
            }
            if self.supports_thinking:
                probe["think"] = False
            warm = await self.client.post("/api/generate", json=probe)
            warm.raise_for_status()
            generated = warm.json()
            if (not isinstance(generated, dict) or generated.get("done") is not True
                or generated.get("done_reason") != "stop" or not isinstance(generated.get("response"), str)):
                raise ProviderError("unavailable", "synthetic generation did not complete")
            answer = json.loads(generated["response"])
            if not isinstance(answer, dict) or set(answer) != {"ready"} or answer["ready"] is not True:
                raise ProviderError("unavailable", "synthetic generation was invalid")
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
        if request.output_mode == "text" and request.purpose != "refine":
            return LlmResponse(None, self.provider_id, self.model_id, finish_reason="error",
                error=ProviderError("invalid_input", "text output is only available for refine"))
        body = {
            "model": self.model_id,
            "stream": False,
            "keep_alive": -1,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": json.dumps(request.input, ensure_ascii=False)},
            ],
            "options": {
                "temperature": 0,
                "num_ctx": self.context_tokens,
                "num_predict": request.max_output_tokens,
            },
        }
        if request.output_mode == "json":
            body["format"] = request.schema
        if self.supports_thinking:
            body["think"] = False
        try:
            async with asyncio.timeout_at(min(deadline, loop_time() + remaining)):
                response = await self.client.post(
                    "/api/chat", json=body, timeout=httpx.Timeout(remaining)
                )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("invalid provider envelope")
            if request.output_mode == "text" and (
                data.get("done") is not True or not isinstance(data.get("done_reason"), str)
            ):
                return LlmResponse(None, self.provider_id, self.model_id, finish_reason="error",
                    error=ProviderError("invalid_output", "text generation did not complete", retryable=True))
            if data.get("done_reason") == "length":
                return LlmResponse(
                    parsed=None,
                    provider=self.provider_id,
                    model_id=self.model_id,
                    finish_reason="length",
                    prompt_tokens=data.get("prompt_eval_count"),
                    completion_tokens=data.get("eval_count"),
                    error=ProviderError("truncated", "output length limit", retryable=False),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            if data.get("done_reason", "stop") != "stop":
                return LlmResponse(parsed=None, provider=self.provider_id, model_id=self.model_id,
                    finish_reason="refusal", prompt_tokens=data.get("prompt_eval_count"),
                    completion_tokens=data.get("eval_count"), error=ProviderError("refused", "provider refused output"))
            content = data.get("message", {}).get("content", "")
            if request.output_mode == "text":
                message = data.get("message", {})
                if (not isinstance(content, str) or not content.strip() or len(content) > 32768
                    or message.get("thinking") or message.get("reasoning_content") or message.get("tool_calls")):
                    return LlmResponse(None, self.provider_id, self.model_id, finish_reason="error",
                        error=ProviderError("invalid_output", "invalid plain-text output", retryable=True))
                return LlmResponse(None, self.provider_id, self.model_id, text=content,
                    prompt_tokens=data.get("prompt_eval_count"), completion_tokens=data.get("eval_count"),
                    elapsed_ms=int((time.monotonic() - started) * 1000))
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                return LlmResponse(parsed=None, provider=self.provider_id, model_id=self.model_id,
                    finish_reason="error", prompt_tokens=data.get("prompt_eval_count"),
                    completion_tokens=data.get("eval_count"),
                    error=ProviderError("invalid_output", "invalid JSON output", retryable=True))
            return LlmResponse(
                parsed=parsed,
                provider=self.provider_id,
                model_id=self.model_id,
                prompt_tokens=data.get("prompt_eval_count"),
                completion_tokens=data.get("eval_count"),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
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
        except (httpx.TimeoutException, TimeoutError) as exc:
            return LlmResponse(
                parsed=None,
                provider=self.provider_id,
                model_id=self.model_id,
                finish_reason="error",
                error=ProviderError("timeout", "LLM request timed out"),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except json.JSONDecodeError as exc:
            return LlmResponse(
                parsed=None,
                provider=self.provider_id,
                model_id=self.model_id,
                finish_reason="error",
                error=ProviderError("invalid_output", str(exc), retryable=True),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            return LlmResponse(
                parsed=None,
                provider=self.provider_id,
                model_id=self.model_id,
                finish_reason="error",
                error=ProviderError("transport_error", str(exc), retryable=True),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    async def health(self) -> bool:
        try:
            response = await self.client.get("/api/tags", timeout=2.0)
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        await self.client.aclose()
