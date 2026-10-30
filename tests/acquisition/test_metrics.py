import pytest
from hpgee.acquisition.metrics import aggregate


def reports():
    return [
        dict(
            rank=rank,
            hosts=3,
            manifest="fixed",
            case_key="same",
            global_required_cells=6,
            target_area_km2=10,
            required_cells=2,
            completed_cells=2,
            complete_local=True,
            required_requests=2,
            failed_requests=0,
            failed_attempts=rank,
            quota_rejections=rank,
            successful_requests=2,
            skipped_requests=0,
            accepted_bytes=100,
            quality_kept_cells=1,
            quality_rejected_cells=1,
            quality_valid_pixels=50,
            quality_clear_pixels=40,
            first_started_epoch=100 + offset,
            started_epoch=100 + offset,
            finished_epoch=120 + offset,
        )
        for rank, offset in enumerate((0, 10, -5))
    ]


def test_global_time_includes_preparation_and_clock_offsets():
    result = aggregate(reports(), clock_offsets={0: 0, 1: 10, 2: -5}, preparation_seconds=5)
    assert result["complete"] and result["completed_cells"] == 6
    assert result["task_wall_seconds"] == 25 and result["seconds_per_km2"] == 2.5
    assert result["quota_rejections"] == 3 and result["accepted_bytes"] == 300


def test_failure_is_not_complete_area_time():
    rows = reports()
    rows[1].update(complete_local=False, failed_requests=1, completed_cells=1)
    result = aggregate(rows, clock_offsets={0: 0, 1: 10, 2: -5})
    assert not result["complete"] and result["seconds_per_km2"] is None


@pytest.mark.parametrize("change", ["missing", "duplicate", "manifest", "case", "count", "clock"])
def test_aggregate_refuses_incomparable_or_missing_results(change):
    rows = reports()
    offsets = {0: 0, 1: 10, 2: -5}
    if change == "missing":
        rows.pop()
    if change == "duplicate":
        rows[2]["rank"] = 1
    if change == "manifest":
        rows[2]["manifest"] = "other"
    if change == "case":
        rows[2]["case_key"] = "other"
    if change == "count":
        rows[2]["required_cells"] = 1
    if change == "clock":
        offsets.pop(2)
    with pytest.raises(ValueError):
        aggregate(rows, clock_offsets=offsets)


def test_resumed_session_rate_excludes_earlier_sessions_and_keeps_quality_counts():
    rows = reports()
    for row in rows:
        # First session began 1000 s earlier; only 20 s of new bytes this session.
        row["first_started_epoch"] -= 1000
    result = aggregate(rows, clock_offsets={0: 0, 1: 10, 2: -5})
    assert result["task_wall_seconds"] == 1020 and result["session_wall_seconds"] == 20
    assert result["accepted_MiB_s"] == 300 / 20 / 1048576
    assert (result["quality_kept_cells"], result["quality_rejected_cells"]) == (3, 3)
    assert (result["quality_valid_pixels"], result["quality_clear_pixels"]) == (150, 120)
