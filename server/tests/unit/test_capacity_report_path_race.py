"""Real owned filesystem swaps, not a trusted capacity/resource attestation."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from app.services import capacity_report
from tests.unit.test_capacity_report import summary


@pytest.mark.parametrize("swap_at", ["root", "parent", "leaf"])
def test_report_open_cannot_follow_a_swapped_directory(tmp_path, monkeypatch, swap_at):
    allowed = tmp_path / "deployment"
    parent = allowed / "nested"
    parent.mkdir(parents=True)
    report = parent / "report.json"
    inside = summary() | {"report_id": "owned-inside"}
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    foreign = summary() | {"report_id": "outside-not-allowed"}
    report.write_text(json.dumps(inside))
    (outside / "report.json").write_text(json.dumps(foreign))
    (outside / "nested/report.json").write_text(json.dumps(foreign))
    opened = []
    swapped = False

    class ObservedOS:
        def __getattr__(self, name):
            return getattr(os, name)

        def open(self, path, flags, *args, **kwargs):
            nonlocal swapped
            target = {"root": allowed.name, "parent": parent.name, "leaf": report.name}[swap_at]
            # Old loader opens one absolute leaf. New confined lookup walks
            # components; swap before exactly the selected component open.
            if not swapped and (Path(path) == report or Path(path).name == target):
                swapped = True
                moved = allowed if swap_at == "root" else parent
                moved.rename(moved.with_name(moved.name + "-held"))
                moved.symlink_to(outside, target_is_directory=True)
            fd = os.open(path, flags, *args, **kwargs)
            opened.append(fd)
            return fd

    monkeypatch.setattr(capacity_report, "os", ObservedOS())
    value = capacity_report.load_capacity_report(str(report), "1" * 64, allowed_roots=[allowed])
    assert swapped, "did not reach the controlled path/open boundary"
    # A held descriptor may still read the original allowed file. Refusal is
    # also safe. It must never certify/read the external replacement.
    assert value == {"status": "unverified", "report": None} or value == {"status": "valid", "report": inside}
    for fd in opened:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF
