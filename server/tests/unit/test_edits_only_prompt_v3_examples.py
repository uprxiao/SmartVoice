"""Prompt examples through actual conversion/guards; no model/audit quality claim."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.core.clock import loop_time
from app.services.llm_validation import matches_schema
from app.services.refine_edits_only import EDITS_ONLY_SCHEMA, freeze_terms, resolve_edits_only
from app.services.refine_evidence import protected_problem, validate_edits


EXAMPLES = json.loads(
    (Path(__file__).parents[1] / 'fixtures/prompt_v3_examples.json').read_text()
)
EXPECTED = {
    'literal_noop': '把标签写成“呃，暂缓出货”，包装不得拆开。',
    'sequence_noop': '先浸泡 7 分钟，再静置 4 分钟。',
    'independent_filler_repeat': '先归档收据，接着核对发票。',
    'correction_complete_constraint': '每批处理 至少 8 份，标签保持原样。',
    'term_position_and_common_meaning': '打开工作台 QuartzDesk 整理笔记，再找 quartz 石英标本。',
    'layout_only': '先核对清单，再封箱',
}


@pytest.mark.asyncio
@pytest.mark.parametrize('example', EXAMPLES, ids=lambda example: example['id'])
async def test_prompt_v3_example_uses_actual_converter_and_protected_guards(example):
    original = deepcopy(example)
    raw, terms, wire = example['input']['transcript'], example['input']['terms'], example['output']
    deadline = loop_time() + 30
    assert matches_schema(wire, EDITS_ONLY_SCHEMA)
    frozen = freeze_terms(terms, deadline=deadline)
    candidate, evidence = resolve_edits_only(raw, wire, frozen, deadline=deadline)
    assert candidate == EXPECTED[example['id']]
    assert [edit['kind'] for edit in evidence] == [edit['kind'] for edit in wire['edits']]
    for edit in evidence:
        assert raw[edit['source_start']:edit['source_end']] == edit['source_text']
    effective, corrections, term_evidence = validate_edits(raw, candidate, evidence, frozen)
    assert protected_problem(effective, candidate) is False
    assert len(corrections) == (1 if example['id'] == 'correction_complete_constraint' else 0)
    assert len(term_evidence) == (1 if example['id'] == 'term_position_and_common_meaning' else 0)
    if term_evidence:
        assert term_evidence[0]['dictionary_term_id'] == terms[0]['id']
        assert '再找 quartz 石英标本' in candidate
    if corrections:
        assert corrections[0]['replacement_text'] == '至少 8 份'
    assert example == original
