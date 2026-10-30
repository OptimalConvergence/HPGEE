"""Only all matching host shards constitute a complete fixed-area task."""

import math


def aggregate(reports, *, clock_offsets, preparation_seconds=0):
    if not reports:
        raise ValueError("Missing host results")
    first = reports[0]
    hosts = first["hosts"]
    ranks = [r["rank"] for r in reports]
    if (
        hosts < 1
        or len(ranks) != hosts
        or set(ranks) != set(range(hosts))
        or set(clock_offsets) != set(ranks)
    ):
        raise ValueError("Every host and its measured clock offset are required")
    if preparation_seconds < 0 or not math.isfinite(preparation_seconds):
        raise ValueError("Invalid preparation time")
    same = ("hosts", "manifest", "case_key", "global_required_cells", "target_area_km2")
    if any(r[k] != first[k] for r in reports for k in same):
        raise ValueError("Incomparable fixed-task results")
    if sum(r["required_cells"] for r in reports) != first["global_required_cells"]:
        raise ValueError("Shard coverage differs from the fixed target")
    for r in reports:
        offset = clock_offsets[r["rank"]]
        if (
            not math.isfinite(offset)
            or r["finished_epoch"] < r["first_started_epoch"]
            or not 0 <= r["completed_cells"] <= r["required_cells"]
        ):
            raise ValueError("Invalid host counts or timestamps")
    started = min(r["first_started_epoch"] - clock_offsets[r["rank"]] for r in reports)
    finished = max(r["finished_epoch"] - clock_offsets[r["rank"]] for r in reports)
    execution = finished - started
    wall = execution + preparation_seconds
    # accepted_bytes are this session's new payloads; resumed sessions skip old
    # blocks, so their rate uses the session window, not the first-start wall.
    session = finished - min(r["started_epoch"] - clock_offsets[r["rank"]] for r in reports)
    complete = all(
        r["complete_local"]
        and not r["failed_requests"]
        and r["completed_cells"] == r["required_cells"]
        for r in reports
    )
    counts = {
        key: sum(r[key] for r in reports)
        for key in (
            "required_cells",
            "completed_cells",
            "required_requests",
            "failed_requests",
            "failed_attempts",
            "quota_rejections",
            "successful_requests",
            "skipped_requests",
            "accepted_bytes",
            "quality_kept_cells",
            "quality_rejected_cells",
            "quality_valid_pixels",
            "quality_clear_pixels",
        )
    }
    return dict(
        counts,
        manifest=first["manifest"],
        case_key=first["case_key"],
        hosts=hosts,
        complete=complete,
        target_area_km2=first["target_area_km2"],
        preparation_seconds=preparation_seconds,
        execution_wall_seconds=execution,
        task_wall_seconds=wall,
        seconds_per_km2=wall / first["target_area_km2"]
        if complete and first["target_area_km2"] is not None and first["target_area_km2"] > 0
        else None,
        session_wall_seconds=session,
        accepted_MiB_s=counts["accepted_bytes"] / max(session, 0.001) / 1048576,
        clock_offsets=clock_offsets,
    )
