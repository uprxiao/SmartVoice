"""Invalid deployment paths must not block an admin request waiting for a peer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX deployment special file')
def test_fifo_report_is_rejected_without_waiting_for_a_writer(tmp_path):
    allowed = tmp_path / 'deployment'
    allowed.mkdir()
    report = allowed / 'report.json'
    os.mkfifo(report, 0o600)
    # A separate real process contains a regression without hanging pytest.
    # The ready line separates import/startup time from the blocked file open.
    code = (
        'import faulthandler,json,sys\n'
        'from pathlib import Path\n'
        'from app.services.capacity_report import load_capacity_report\n'
        'faulthandler.dump_traceback_later(1, repeat=False)\n'
        'print("entering-loader",flush=True)\n'
        'value=load_capacity_report(sys.argv[1],"1"*64,allowed_roots=[Path(sys.argv[2])])\n'
        'faulthandler.cancel_dump_traceback_later()\n'
        'print(json.dumps(value),flush=True)\n'
    )
    child = subprocess.Popen(
        [sys.executable, '-c', code, str(report), str(allowed)],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert child.stdout is not None
    timed_out = False
    try:
        ready, _, _ = select.select([child.stdout], [], [], 5)
        assert ready and child.stdout.readline().strip() == 'entering-loader', 'loader subprocess did not become ready'
        try:
            out, err = child.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Release only this owned FIFO so the broken reader can exit
            # normally and retain its original stack. Never send it data.
            writer = os.open(report, os.O_WRONLY | os.O_NONBLOCK)
            os.close(writer)
            out, err = child.communicate(timeout=5)
        assert child.returncode == 0, err
        assert not timed_out, 'capacity loader waited for a FIFO writer; actual child stack:\n' + err
        assert json.loads(out) == {'status': 'unverified', 'report': None}
    finally:
        if child.poll() is None:
            child.terminate()
            child.communicate(timeout=5)
