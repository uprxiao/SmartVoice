"""Bound the actual model-visible terms JSON, across both real adapters."""
import asyncio
import json

import httpx
import pytest

from app.core.clock import loop_time
from app.providers.llm.base import LlmRequest
from app.providers.llm.ollama import OllamaProvider
from app.providers.llm.openai_compatible import OpenAICompatibleProvider
from app.services.dictionary_prompt import refine_hints


@pytest.mark.parametrize('provider_name', ['ollama', 'openai_compatible'])
def test_dictionary_budget_bounds_model_visible_json(provider_name):
    async def run():
        snapshot = [dict(
            id=f'00000000-0000-4000-8000-{i:012d}', term=f'Term{i}',
            norm=f'term{i}', aliases=[f'alias{i}'], contexts=[],
            updated_timestamp=float(i),
        ) for i in range(50)]
        raw = ' '.join(f'alias{i}' for i in range(50))
        selected = refine_hints(snapshot, raw)
        assert 0 < len(selected) <= 50
        captured = []

        def respond(request):
            captured.append(json.loads(request.content))
            if provider_name == 'ollama':
                return httpx.Response(200, json={
                    'done_reason': 'stop', 'message': {'content': '{"ok":true}'},
                })
            return httpx.Response(200, json={'choices': [{
                'finish_reason': 'stop', 'message': {'content': '{"ok":true}'},
            }]})

        provider = (OllamaProvider('http://fixture.invalid', 'fixture')
                    if provider_name == 'ollama' else
                    OpenAICompatibleProvider('http://fixture.invalid', 'fixture', None))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(
            base_url='http://fixture.invalid', transport=httpx.MockTransport(respond),
            trust_env=False,
        )
        try:
            response = await provider.generate(LlmRequest(
                purpose='refine', system_prompt='Synthetic wire-boundary check.',
                input={'transcript': raw, 'terms': selected},
                schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                        'required': ['ok'], 'additionalProperties': False},
                max_output_tokens=1024,
            ), loop_time() + 30)
            assert response.error is None and response.parsed == {'ok': True}
            assert len(captured) == 1
            content = captured[0]['messages'][1]['content']
            assert json.loads(content) == {'transcript': raw, 'terms': selected}
            # Extract the exact terms slice of the actual adapter's message,
            # including its separators; reserializing it compactly hides the bug.
            start = content.index(':', content.index('"terms"')) + 1
            start += len(content[start:]) - len(content[start:].lstrip())
            decoded, length = json.JSONDecoder().raw_decode(content[start:])
            assert decoded == selected
            assert len(content[start:start + length]) <= 4000
        finally:
            await provider.close()
        assert provider.client.is_closed

    asyncio.run(run())
