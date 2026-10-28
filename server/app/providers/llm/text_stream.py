"""Bounded OpenAI-compatible SSE text output; never publishes a partial body."""
from __future__ import annotations

import json

from ..asr.base import ProviderError

MAX_TEXT = 32768
MAX_EVENT = 262144
MAX_WIRE = 1048576


def invalid():
    return ProviderError('invalid_output', 'incomplete or invalid text stream', retryable=True)


async def read_text_stream(response):
    parts, data_lines, used, wire, event_size = [], [], 0, 0, 0
    finished = False
    usage = {}

    def consume(data):
        nonlocal finished, usage, used
        if data == '[DONE]':
            if not finished or not parts or not ''.join(parts).strip():
                raise invalid()
            return True
        try:
            value = json.loads(data)
        except (ValueError, TypeError):
            raise invalid() from None
        if not isinstance(value, dict) or 'error' in value:
            raise invalid()
        choices = value.get('choices')
        if not isinstance(choices, list):
            raise invalid()
        if value.get('usage') is not None:
            if not isinstance(value['usage'], dict):
                raise invalid()
            usage = value['usage']
        if not choices:
            if value.get('usage') is None:
                raise invalid()
            return False
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise invalid()
        choice = choices[0]
        if type(choice.get('index')) is not int or choice['index'] != 0 or finished:
            raise invalid()
        delta = choice.get('delta')
        if not isinstance(delta, dict):
            raise invalid()
        if delta.get('refusal'):
            raise ProviderError('refused', 'provider refused output')
        if delta.get('reasoning_content'):
            raise invalid()  # Thinking is disabled, never merge it into text.
        if delta.get('tool_calls') or delta.get('function_call'):
            raise invalid()
        content = delta.get('content')
        if content is not None:
            if not isinstance(content, str):
                raise invalid()
            used += len(content)
            if used > MAX_TEXT:
                raise invalid()
            parts.append(content)
        finish = choice.get('finish_reason')
        if finish is not None:
            if finish == 'length':
                raise ProviderError('truncated', 'output length limit')
            if finish != 'stop':
                raise ProviderError('refused', 'provider did not complete text')
            finished = True
        return False

    # httpx incrementally decodes UTF-8 and CRLF, including split byte sequences.
    # The enclosing provider owns the absolute timeout, not an idle-per-line one.
    async for line in response.aiter_lines():
        wire += len(line) + 1
        if wire > MAX_WIRE or len(line) > MAX_EVENT:
            raise invalid()
        if not line:
            if data_lines:
                done = consume('\n'.join(data_lines))
                data_lines.clear()
                event_size = 0
                if done:
                    return ''.join(parts), usage
            continue
        if line.startswith(':'):
            continue
        field, _, value = line.partition(':')
        if field == 'data':
            data_lines.append(value[1:] if value.startswith(' ') else value)
            event_size += len(data_lines[-1]) + 1
            if event_size > MAX_EVENT:
                raise invalid()
        # event/id/retry are SSE envelope metadata, not model content.
    raise invalid()  # EOF (even after stop) is not a successful [DONE] event.
