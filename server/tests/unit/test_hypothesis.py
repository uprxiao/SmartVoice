from __future__ import annotations

from app.vendor.hypothesis import HypothesisBuffer


def words(*items):
    return [(float(i), float(i + 1), word) for i, word in enumerate(items)]


def test_flush_commits_common_prefix_only():
    buffer = HypothesisBuffer(logfile=None)
    buffer.insert(words("hello", "world"), 0)
    assert buffer.flush() == []

    buffer.insert(words("hello", "world", "again"), 0)
    commit = buffer.flush()
    assert [item[2] for item in commit] == ["hello", "world"]
    assert [item[2] for item in buffer.complete()] == ["again"]


def test_flush_does_not_commit_unstable_tail():
    buffer = HypothesisBuffer(logfile=None)
    buffer.insert(words("alpha", "beta"), 0)
    buffer.flush()
    buffer.insert(words("alpha", "beta", "gamma"), 0)
    commit = buffer.flush()
    assert [item[2] for item in commit] == ["alpha", "beta"]
    assert [item[2] for item in buffer.complete()] == ["gamma"]


def test_insert_applies_sample_offset():
    buffer = HypothesisBuffer(logfile=None)
    buffer.insert(words("offset", "word"), 16000)
    buffer.flush()
    buffered = buffer.complete()
    assert buffered[0][0] == 16000.0
