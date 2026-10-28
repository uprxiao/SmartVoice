"""Read-only origin binding for the fixed retry lease, with no clock changes."""
from tests.integration.test_capacity_retry_cache_real600 import clock_sample


LEASE_FIELDS = ("lease_deadline_loop", "lease_expiry_epoch", "lease_origin_lower",
                "lease_origin_upper")


def observe_fixed_lease(manager, observer, monkeypatch):
    origins = {}
    arm, select = manager._arm_audio_lease, manager._asr_deadline

    def armed(runtime, expires_at):
        before = clock_sample()
        lease = arm(runtime, expires_at)
        after = clock_sample()
        if id(lease) not in origins:
            origins[id(lease)] = {
                "lease_deadline_loop": lease.deadline,
                "lease_expiry_epoch": lease.expires_at,
                "lease_origin_lower": before["loop_before"] + max(0, expires_at - after["epoch"]),
                "lease_origin_upper": after["loop_after"] + max(0, expires_at - before["epoch"]),
            }
            observer.record("lease_origin_observed", session_id=runtime.session_id,
                            origin_before=before, origin_after=after, **origins[id(lease)])
        return lease

    def selected(runtime):
        start = len(observer.rows)
        deadline = select(runtime)
        if runtime.retry_expires_at is not None:
            lease = runtime.audio_lease
            assert lease is not None and id(lease) in origins, "lease creation was not observed"
            origin = origins[id(lease)]
            assert lease.deadline == origin["lease_deadline_loop"]
            assert lease.expires_at == origin["lease_expiry_epoch"] == runtime.retry_expires_at
            for row in observer.rows[start:]:
                if row["kind"] == "asr_deadline_selected" and row.get("session_id") == runtime.session_id:
                    row.update(origin)
        return deadline

    monkeypatch.setattr(manager, "_arm_audio_lease", armed)
    monkeypatch.setattr(manager, "_asr_deadline", selected)


def assert_selected_original_lease(row, expiry):
    assert row["lease_expiry_epoch"] == expiry
    assert row["lease_origin_lower"] <= row["lease_deadline_loop"] <= row["lease_origin_upper"]
    assert row["selected_deadline_loop"] == row["lease_deadline_loop"]
