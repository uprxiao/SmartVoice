"""Outward UTC formatter policy only; no global text/date serialization."""
import datetime as dt

import pytest


@pytest.mark.parametrize('source', [
    dt.datetime(2026, 10, 19, 3, 4, 5, 123456, tzinfo=dt.timezone.utc),
    dt.datetime(2026, 10, 19, 11, 4, 5, 123456, tzinfo=dt.timezone(dt.timedelta(hours=8))),
], ids=['utc-microseconds', 'offset-same-instant'])
def test_outward_datetime_preserves_precise_utc_instant(source):
    from app.core.wire_time import utc_iso8601
    assert utc_iso8601(source) == '2026-10-19T03:04:05.123456Z'
    assert source.microsecond == 123456  # No mutation/truncation of the input.


def test_outward_datetime_rejects_unproven_naive_zone():
    from app.core.wire_time import utc_iso8601
    with pytest.raises(ValueError):
        utc_iso8601(dt.datetime(2026, 10, 19, 3, 4, 5, 123456))
