"""Draft for root to copy into server/tests/unit after the frozen full run.

Synthetic text only; public HypothesisBuffer methods, no model/audio/database.
"""
from __future__ import annotations

import logging
import re

import pytest

from app.vendor import hypothesis


@pytest.mark.parametrize("duplicate_count", [1, 5], ids=["one-word", "five-word-ngram"])
def test_debug_dedup_keeps_text_private_and_preserves_following_commit(
    caplog, monkeypatch, duplicate_count
):
    # Enable the original module logger: silencing DEBUG is not a valid fix.
    monkeypatch.setattr(hypothesis.logger, "disabled", False)
    monkeypatch.setattr(hypothesis.logger, "propagate", True)
    caplog.set_level(logging.DEBUG, logger=hypothesis.logger.name)
    private_words = [f"synthetic-private-transcript-{i}" for i in range(duplicate_count)]
    initial = [(i / 10, (i + 1) / 10, word) for i, word in enumerate(private_words)]
    buffer = hypothesis.HypothesisBuffer(logfile=None)

    # Two equal hypotheses commit their common prefix through the public API.
    buffer.insert(initial, 0)
    assert buffer.flush() == []
    buffer.insert(initial, 0)
    assert buffer.flush() == initial
    assert buffer.complete() == []
    caplog.clear()

    # Fresh timestamps pass the original time filter. Unique tokens force the
    # full requested n-gram match (shorter suffix/prefix matches cannot succeed).
    end = initial[-1][1]
    repeated = [(end + .01 + i / 10, end + .11 + i / 10, word)
                for i, word in enumerate(private_words)]
    tail_start = end + 1
    tail = [(tail_start, tail_start + .1, "synthetic-private-following-tail")]
    buffer.insert(repeated + tail, 0)
    assert buffer.flush() == []
    assert buffer.complete() == tail

    records = [record for record in caplog.records if record.name == hypothesis.logger.name]
    assert len(records) == 1, "the original DEBUG dedup event must remain observable"
    assert records[0].levelno == logging.DEBUG
    # Check privacy before wording: the old-source negative control must fail
    # because a transcript token leaked, rather than only on the message format.
    # Formatting later must not reintroduce text held in LogRecord.msg/args.
    logged = "\n".join((caplog.text, str(records[0].msg), repr(records[0].args)))
    for word in private_words + [tail[0][2]]:
        assert word not in logged
    match = re.fullmatch(r"removing (\d+) duplicate words", records[0].getMessage())
    assert match is not None
    assert int(match.group(1)) == duplicate_count

    # The privacy fix must preserve the next stable hypothesis and exact-once
    # public commit behavior; no direct mutation of buffer internals is used.
    buffer.insert(tail, 0)
    assert buffer.flush() == tail
    assert buffer.complete() == []
    assert buffer.flush() == []
