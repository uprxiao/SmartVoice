"""Real API/PG drafts; all provider computation is explicitly synthetic.

No claim of Desktop collection, real model decisions, or physical model usage.
A willing fake classifier makes a protected-path bypass observable.
"""
import time
import uuid
import json

import pytest

from app.services.classifier import Classification
from tests.cases.test_learn_cases import prepare_inserted


@pytest.mark.parametrize("source,marker,before,after,blocked", [
    pytest.param("路径 /tmp/a", "/tmp/", "a", "b", True, id="inside-path-omitted-context"),
    pytest.param("路径/tmp/a", "/tmp/", "a", "b", True, id="chinese-adjacent-multi-segment-root"),
    pytest.param("路径 /tmp/a，代号 a 已冻结", "/tmp/", "a", "b", True, id="same-text-inside-path"),
    pytest.param("路径 /tmp/a，代号 a 已冻结", "代号 ", "a", "b", False, id="same-text-outside-path"),
    pytest.param("保存到 /cache", "保存到 ", "/cache", "/state", True, id="root-without-extension"),
    pytest.param("访问https://example.test/alpha", "example.test/", "alpha", "bravo", True, id="url-inner-fragment"),
    pytest.param("使用 CI/CB 流水线", "使用 ", "CI/CB", "CI/CD", False, id="ordinary-slash-term"),
    pytest.param("模式为输入/输处", "输入/", "输处", "输出", False, id="ordinary-chinese-slash-term"),
    pytest.param("同事叫吴非", "同事叫", "吴非", "吴菲", False, id="name-no-new-negation-rule"),
    pytest.param("设备品牌是小咪", "设备品牌是", "小咪", "小米", False, id="unit-character-in-brand"),
])
def test_path_guard_uses_owned_text_and_exact_offset(
    app_client, monkeypatch, record_property, source, marker, before, after, blocked,
):
    calls = []

    async def willing_classifier(before, after, left, right):
        calls.append((before, after, left, right))
        return Classification(decision="term", ambiguous=False, category="spelling",
            evidence={"before_start": 0, "before_end": len(before), "before": before, "after": after})

    monkeypatch.setattr(app_client.app.state.classifier, "classify", willing_classifier)
    headers, session, result, insertion_id = prepare_inserted(app_client, source, "path-source")
    text = result["refined_text"]
    assert text.count(source) == 1, "Source was changed; do not silently adapt the fixture"
    relative_offset = source.index(marker) + len(marker)
    assert source[relative_offset:relative_offset + len(before)] == before
    offset = text.index(source) + relative_offset
    response = app_client.post("/api/v1/corrections", headers=headers, json={
        "session_id": session["session_id"], "insertion_id": insertion_id,
        "correction_id": str(uuid.uuid4()), "offset": offset, "before": before, "after": after,
        # Intentionally omit left/right: even truthful client context is optional.
    })
    assert response.status_code == 202, response.text
    event = response.json()
    # Also wait on the old vulnerable implementation, to observe its actual
    # terminal event and persisted dictionary rather than just a pending reply.
    # Keep the 10s observation bound and <40 reads, with production limits on.
    deadline = time.monotonic() + 10
    while event["status"] == "pending" and time.monotonic() < deadline:
        time.sleep(0.3)
        fetched = app_client.get(f"/api/v1/learning-events/{event['event_id']}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        event = fetched.json()
    assert event["status"] != "pending", "synthetic classifier did not finish"
    terms = app_client.get("/api/v1/dictionary/terms", headers=headers)
    assert terms.status_code == 200, terms.text
    record_property("terminal_status", event["status"])
    record_property("classification_calls", len(calls))
    record_property("dictionary_version", terms.json()["dictionary_version"])
    record_property("term_names", json.dumps([t["term"] for t in terms.json()["items"]], ensure_ascii=False))
    if blocked:
        assert event["status"] == "ignored"
        assert response.json()["status"] == "ignored"
        assert calls == []
        assert terms.json()["items"] == []
        assert terms.json()["dictionary_version"] == 0
    else:
        assert event["status"] == "learned", event
        assert calls == [(before, after, "", "")]
        assert len(terms.json()["items"]) == 1
        assert terms.json()["items"][0]["term"] == after
        assert before in terms.json()["items"][0]["aliases"]
