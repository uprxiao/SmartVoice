"""Real correction/API/PG with scripted physical JSON; no model-quality claim."""
from copy import deepcopy

import pytest

from app.providers.llm.base import LlmResponse
from app.services.classifier import ClassifierService
from tests.cases.test_dict_cases import fetch_one
from tests.cases.test_learn_cases import (
    attach_insertion, list_dict, prepare_inserted, submit, wait_event,
)


class DecisionProvider:
    provider_id, model_id = 'scripted', 'classification-combination-guard'

    def __init__(self, ambiguous):
        self.ambiguous, self.calls = ambiguous, []

    async def generate(self, request, deadline):
        self.calls.append((request, deadline))
        return LlmResponse(provider=self.provider_id, model_id=self.model_id, parsed={
            'decision': 'suggest', 'ambiguous': self.ambiguous, 'category': 'spelling',
            'reason_code': 'FICTIONAL_PROTOCOL_CONTROL',
            'evidence': deepcopy(request.input['expected_evidence']),
        })


@pytest.mark.parametrize('ambiguous,before,after,expected', [
    pytest.param(False, '按钮', '开关', 'failed', id='contradictory-suggest-fails-closed'),
    pytest.param(True, '云', '芸', 'suggested', id='valid-ambiguous-suggestion-not-auto-learned'),
])
def test_correction_combination_guard_prevents_dictionary_write(
    app_client, monkeypatch, ambiguous, before, after, expected,
):
    headers, session, result, insertion_id = prepare_inserted(
        app_client, '虚构词语'+before+'，仅用于测试。', 'decision-guard-'+str(ambiguous).lower())
    attach_insertion(session, insertion_id)
    provider = DecisionProvider(ambiguous)
    monkeypatch.setattr(app_client.app.state, 'classifier', ClassifierService(provider))

    response = submit(app_client, headers, session, result, before, after)
    assert response.status_code == 202
    event_id = response.json()['event_id']
    event = wait_event(app_client, headers, event_id)
    assert event['status'] == expected
    assert event['reason'] == ('INVALID_CLASSIFICATION' if not ambiguous else None)
    assert list_dict(app_client, headers)['items'] == []
    assert fetch_one("select count(*) from learning_events where status='learned'") == 0
    assert len(provider.calls) == 1
    # A second independent API read must retain the same terminal failure/status.
    again = app_client.get('/api/v1/learning-events/'+event_id, headers=headers)
    assert again.status_code == 200 and again.json()['status'] == expected
