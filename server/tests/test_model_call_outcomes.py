from app.core.clock import loop_time

from sqlalchemy import select

from app.models import ModelCall, ModelUsageDaily
from app.providers.asr.base import ProviderError
from app.providers.llm.base import LlmRequest, LlmResponse
from app.services.inference_context import CallScope, call_scope
from tests.conftest import auth_headers, register_and_login
from tests.test_sessions import create_session


def test_dispatched_timeout_is_local_failure_with_unknown_remote_use(app_client, monkeypatch):
    tokens = register_and_login(app_client)
    session = create_session(app_client, auth_headers(tokens), 'usage-timeout-outcome').json()
    state = app_client.app.state
    executions = []

    async def timeout(request, deadline):
        executions.append(request.call_id)
        return LlmResponse(parsed=None, provider='mock', model_id='mock-llm',
            error=ProviderError('timeout', 'controlled provider timeout'))

    monkeypatch.setattr(state.llm.inner, 'generate', timeout)

    async def exercise():
        with call_scope(CallScope(tokens['user']['id'], session['session_id'], 1)):
            response = await state.llm.generate(LlmRequest(purpose='refine',
                system_prompt='test-only', input={}, schema={'type': 'object'}), loop_time() + 5)
        assert response.error is not None and response.error.code == 'timeout'
        assert len(executions) == 1 and executions[0]
        # Replaying terminal ledger completion must not count the physical call twice.
        await state.recorder.finished(executions[0], 'unknown', 'timeout', 1)
        async with state.db.session_factory() as db:
            call = await db.get(ModelCall, executions[0])
            assert call.status == 'unknown'
            assert call.prompt_tokens is None and call.completion_tokens is None
            usage = await db.scalar(select(ModelUsageDaily).where(
                ModelUsageDaily.user_id == tokens['user']['id'], ModelUsageDaily.purpose == 'refine'))
            assert usage.calls == 1 and usage.successes == 0
            assert usage.failures == 1  # Local transport failed; remote execution is unknown.
            assert usage.unknown_calls == 1
            assert usage.prompt_tokens_unknown_calls == usage.completion_tokens_unknown_calls == 1

    app_client.portal.call(exercise)
