"""Classifier request/evidence regressions; scripted responses are not model QA.

Real L01/L06/L07 semantics require the fixed-fixture real-model/API experiment.
These tests never infer classification quality from prompt text or a stub vote.
"""
from copy import deepcopy

import pytest

from app.providers.llm.base import LlmResponse
from app.services.classifier import ClassifierService


class ScriptedProvider:
    provider_id, model_id = "stub", "classifier-contract"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def generate(self, request, deadline):
        self.calls.append((request, deadline))
        return LlmResponse(
            parsed=deepcopy(self.answers.pop(0)),
            provider=self.provider_id,
            model_id=self.model_id,
        )


def scripted_term(evidence):
    # A scripted result exercises transport/validation, not semantic eligibility.
    return {
        "decision": "term",
        "ambiguous": False,
        "category": "spelling",
        "reason_code": "SCRIPTED_CONTRACT_RESPONSE",
        "evidence": evidence,
    }


@pytest.mark.parametrize(
    "before,after,left,right,codepoints",
    [
        pytest.param("SQL lite", "SQLite", "这个项目使用 ", " 存储配置", 8, id="l01-exact-spaces"),
        pytest.param("🙂SQL lite", "🙂SQLite", " 名称 ", " 配置 ", 9, id="non-bmp-is-one-codepoint"),
        pytest.param("e\u0301Lite", "ÉLite", " 名称 ", " 配置 ", 6, id="combining-mark-not-normalized"),
    ],
)
async def test_request_template_preserves_exact_text_and_unicode_codepoints(
    before, after, left, right, codepoints
):
    expected = {
        "before_start": 0,
        "before_end": codepoints,
        "before": before,
        "after": after,
    }
    provider = ScriptedProvider(scripted_term(expected))

    answer = await ClassifierService(provider).classify(before, after, left, right)

    assert len(provider.calls) == 1
    request, _ = provider.calls[0]
    assert request.purpose == "classify"
    assert request.output_schema_id == "classify-v1"
    assert request.input == {
        "before": before,
        "after": after,
        "left": left,
        "right": right,
        "expected_evidence": expected,
    }
    assert answer.reason_code is None
    assert answer.evidence == expected
    assert answer.decision == "term"  # Only verifies a valid scripted response is not blanket-rejected.


@pytest.mark.parametrize(
    "before,after,correct_end,changed_field,wrong_value",
    [
        pytest.param("SQL lite", "SQLite", 8, "before_start", 1, id="wrong-start"),
        pytest.param("SQL lite", "SQLite", 8, "before_end", 7, id="l01-space-not-counted"),
        pytest.param("SQL lite", "SQLite", 8, "before", "SQLlite", id="space-stripped-from-before"),
        pytest.param("SQL lite", "SQLite", 8, "after", "Sqlite", id="after-case-changed"),
        pytest.param("🙂SQL lite", "🙂SQLite", 9, "before_end", 10, id="utf16-units-not-codepoints"),
        pytest.param("e\u0301Lite", "ÉLite", 6, "before", "éLite", id="normalized-before-not-exact"),
    ],
)
async def test_schema_valid_wrong_evidence_is_rejected_without_repair_or_semantic_retry(
    before, after, correct_end, changed_field, wrong_value
):
    correct = {"before_start": 0, "before_end": correct_end, "before": before, "after": after}
    wrong = dict(correct)
    wrong[changed_field] = wrong_value
    # A valid second answer is deliberately available: consuming it would hide
    # the invalid evidence through an unauthorized semantic retry.
    provider = ScriptedProvider(scripted_term(wrong), scripted_term(correct))

    answer = await ClassifierService(provider, automatic_retries=1).classify(before, after, "", "")

    assert answer.reason_code == "EVIDENCE_MISMATCH"
    assert answer.decision == "ignore"
    assert answer.evidence == {}
    assert len(provider.calls) == 1
    assert len(provider.answers) == 1


async def test_allowed_schema_retry_does_not_enable_a_third_evidence_repair_call():
    correct = {"before_start": 0, "before_end": 8, "before": "SQL lite", "after": "SQLite"}
    malformed = scripted_term(correct)
    malformed["ambiguous"] = "false"
    wrong = scripted_term(dict(correct, before_end=7))
    provider = ScriptedProvider(malformed, wrong, scripted_term(correct))

    answer = await ClassifierService(provider, automatic_retries=1).classify(
        "SQL lite", "SQLite", "这个项目使用 ", " 存储配置"
    )

    assert answer.reason_code == "EVIDENCE_MISMATCH"
    assert answer.evidence == {}
    assert len(provider.calls) == 2
    assert len(provider.answers) == 1
    assert provider.calls[0][1] == provider.calls[1][1]
    assert provider.calls[0][0].input == provider.calls[1][0].input


async def test_input_template_does_not_expand_the_output_schema():
    correct = {"before_start": 0, "before_end": 8, "before": "SQL lite", "after": "SQLite"}
    extra_output_field = scripted_term(correct)
    extra_output_field["expected_evidence"] = deepcopy(correct)
    provider = ScriptedProvider(extra_output_field, extra_output_field, scripted_term(correct))

    answer = await ClassifierService(provider, automatic_retries=1).classify("SQL lite", "SQLite", "", "")

    assert answer.reason_code == "MODEL_BUSY"
    assert answer.evidence == {}
    assert len(provider.calls) == 2
    assert len(provider.answers) == 1
    assert provider.calls[0][1] == provider.calls[1][1]


@pytest.mark.parametrize('before,after', [('按钮', '开关'), ('SQL lite', 'SQLite')])
async def test_nonambiguous_suggestion_fails_closed_without_semantic_retry(before, after):
    evidence = {'before_start': 0, 'before_end': len(before), 'before': before, 'after': after}
    contradictory = scripted_term(evidence)
    contradictory['decision'] = 'suggest'
    provider = ScriptedProvider(contradictory, scripted_term(evidence))

    answer = await ClassifierService(provider).classify(before, after, '', '')

    assert answer.decision == 'ignore'
    assert answer.reason_code == 'INVALID_CLASSIFICATION'
    assert answer.evidence == {}
    assert len(provider.calls) == 1 and len(provider.answers) == 1
