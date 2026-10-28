from __future__ import annotations

import pytest

from app.services.refiner_core import (
    correction_guard,
    fidelity_problem,
    format_explicit_list,
    substantive,
    verified_omissions,
)


def edit(kind: str, source: str, replacement: str = "") -> dict:
    return {"kind": kind, "source": source, "replacement": replacement}


def test_filler_removal_is_verified_but_needs_review():
    original = "嗯那个，帮我加一个按钮。"
    effective, review = verified_omissions(
        original, [edit("filler", "嗯那个，")], "帮我加一个按钮。"
    )
    assert effective == "帮我加一个按钮。"
    assert review is True


def test_invented_source_is_rejected():
    with pytest.raises(ValueError):
        verified_omissions("帮我加一个按钮", [edit("filler", "删掉这句话")], "按钮")


def test_overlapping_sources_are_rejected():
    original = "然后然后帮我加按钮"
    with pytest.raises(ValueError):
        verified_omissions(
            original,
            [edit("repeat", "然后然后", "然后"), edit("filler", "然后帮我")],
            "然后帮我加按钮",
        )


def test_repeat_must_actually_remove_adjacent_duplicate():
    with pytest.raises(ValueError):
        verified_omissions(
            "测试测试一下", [edit("repeat", "测试测试", "测试测试")], "测试测试一下"
        )


def test_self_correction_requires_marker():
    with pytest.raises(ValueError):
        verified_omissions(
            "用红色蓝色",
            [edit("self_correction", "用红色蓝色", "蓝色")],
            "蓝色",
        )


def test_self_correction_keeps_final_statement():
    original = "用红色，不对，用蓝色"
    effective, review = verified_omissions(
        original,
        [edit("self_correction", "用红色，不对，用蓝色", "用蓝色")],
        "用蓝色",
    )
    assert effective == "用蓝色"
    assert review is True


def test_fidelity_detects_lost_negation():
    assert fidelity_problem("加一个按钮，不要修改数据库", "加一个按钮") is not None


def test_fidelity_detects_lost_number_and_path():
    assert fidelity_problem("改 3 个文件，备份到 ~/backup/a", "改几个文件") is not None


def test_explicit_list_layout_preserves_words():
    text = "我有三个要求：加一个按钮，把颜色改成蓝色，不要修改数据库。"
    formatted = format_explicit_list(text)
    assert formatted.count("\n- ") == 3
    assert substantive(formatted) == substantive(text)
    assert "不要修改数据库" in formatted


def test_explicit_list_ignores_ambiguous_counts():
    text = "我有三个要求：加按钮，改颜色。"
    assert format_explicit_list(text) == text


def test_correction_guard_accepts_simple_term():
    assert correction_guard("cloud", "claw") is None
    assert correction_guard("库", "仓库") is None


def test_correction_guard_rejects_numbers_negation_and_sentences():
    assert correction_guard("3", "2") is not None
    assert correction_guard("不要修改", "修改") is not None
    assert correction_guard("改颜色。", "改颜色") is not None
    assert correction_guard("a", "b" * 200) is not None
