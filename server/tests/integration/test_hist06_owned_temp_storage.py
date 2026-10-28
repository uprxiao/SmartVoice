"""One real API/PG lifecycle case; Mock physical provider, owned temp directory."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

import pytest

from tests.cases import test_hist_cases as original


@pytest.fixture
def owned_audio_temp(monkeypatch, tmp_path):
    # This fixture intentionally does not depend on app_client. The test asks
    # for that existing fixture only after both temporary-directory selectors
    # are set. Do not run this node concurrently inside the same Python process.
    root = tmp_path / "owned-audio-temp"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(root))
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    identity = root.stat().st_dev, root.stat().st_ino
    assert tempfile.gettempdir() == str(root)
    return root, identity


def test_hist06_original_lifecycle_has_no_owned_temp_files(
    request, monkeypatch, owned_audio_temp, record_property,
):
    # Reuse the original business test and all of its existing assertions.
    source = Path(original.__file__)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        "f266fde6912f0445f25179c220dfaeac7710da3bf756d17dc7d1fcc071b15603"
    ), "original HIST06 changed; review and re-pin before running this wrapper"
    root, identity = owned_audio_temp
    observations = []

    def check_empty(stage):
        assert os.environ["TMPDIR"] == str(root)
        assert tempfile.tempdir == tempfile.gettempdir() == str(root)
        assert not root.is_symlink() and root.is_dir()
        current = root.stat()
        assert (current.st_dev, current.st_ino) == identity
        # Empty means no audio, unknown file, subdirectory or symlink. Never
        # traverse another directory or include discovered names/content.
        count = sum(1 for _ in root.iterdir())
        observations.append({"stage": stage, "entry_count": count})
        assert count == 0, f"owned temp directory is nonempty at {stage}"

    try:
        check_empty("before_app_client")
        client = request.getfixturevalue("app_client")
        assert client.app.state.config.asr.provider == "mock"
        assert client.app.state.config.llm.provider == "mock"
        check_empty("after_readiness")
        expected = (("success", "ready"), ("empty", "empty"),
                    ("cancelled", "cancelled"), ("incomplete", "failed"))
        released = []
        original_release_assert = original.assert_audio_released

        def release_assert(app_client, owner):
            original_release_assert(app_client, owner)
            assert len(released) < len(expected)
            stage, state = expected[len(released)]
            assert owner["runtime"].state == state
            check_empty(stage + "_released")
            released.append(stage)

        monkeypatch.setattr(original, "assert_audio_released", release_assert)
        original.test_tc_hist_06_audio_lifecycle_and_storage(client, monkeypatch)
        assert released == [stage for stage, _ in expected]
        # The original case has now asserted a complete failed ASR attempt
        # retains its original nonempty bytearray, retry eligibility and DB
        # metadata. Check storage while that cache is still alive, before close.
        check_empty("complete_asr_failure_cache_in_memory")
    finally:
        record_property("hist06_owned_temp_storage", json.dumps({
            "scope": "owned_TMPDIR_at_lifecycle_checkpoints",
            "physical_provider": "mock", "checks": observations,
            "not_a_global_or_transient_write_audit": True,
        }, sort_keys=True))
