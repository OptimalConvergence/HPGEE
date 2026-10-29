from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests.exceptions import SSLError

from hpgee.catalog.catalog import S2SceneCatalog
from hpgee.catalog.metadata import MetadataScanner, grid_key, scan_status
from hpgee.runtime.retry import retry_read

COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"


def asset(index="a", date="2025-01-01T01:00:00Z"):
    return {
        "id": COLLECTION + "/" + index,
        "startTime": date,
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
        "properties": {"MGRS_TILE": "31NAA", "CLOUDY_PIXEL_PERCENTAGE": 20},
    }


def scanner(tmp_path, fetch):
    return MetadataScanner(
        S2SceneCatalog(tmp_path / "catalog.sqlite"),
        SimpleNamespace(data=SimpleNamespace(listImages=fetch)),
    )


def test_atomic_page_resume_and_incremental_noop(tmp_path):
    fetch = Mock(
        side_effect=[{"images": [asset()], "nextPageToken": "p2"}, RuntimeError("interrupted")]
    )
    scan = scanner(tmp_path, fetch)
    with pytest.raises(RuntimeError):
        scan.sync("s2", "2025-01-01", "2025-01-03", workers=1)
    assert scan_status(scan.catalog.path)["collections"][0]["completed_windows"] == 0
    fetch.side_effect = None
    fetch.return_value = {"images": [asset("b")]}
    scan.sync("s2", "2025-01-01", "2025-01-03", workers=1)
    assert fetch.call_args.args[0]["pageToken"] == "p2"
    assert scan.catalog.stats().scenes == 2
    calls = fetch.call_count
    assert scan.sync("s2", "2025-01-01", "2025-01-03")["complete"]
    assert fetch.call_count == calls


def test_empty_window_is_complete(tmp_path):
    scan = scanner(tmp_path, Mock(return_value={"images": []}))
    assert scan.sync("landsat4", "2000-01-01", "2000-02-01")["complete"]
    assert scan.catalog.stats().scenes == 0
    assert scan_status(scan.catalog.path)["collections"][0]["completed_windows"] == 1


def test_invalid_page_does_not_advance_or_insert(tmp_path):
    bad = asset("bad")
    bad.pop("geometry")
    scan = scanner(tmp_path, Mock(return_value={"images": [asset(), bad]}))
    with pytest.raises(ValueError, match="footprint"):
        scan.sync("s2", "2025-01-01", "2025-01-03")
    assert scan.catalog.stats().scenes == 0
    assert scan_status(scan.catalog.path)["collections"][0]["pages"] == 0


def test_repeated_cursor_fails_without_claiming_coverage(tmp_path):
    scan = scanner(tmp_path, Mock(return_value={"images": [asset()], "nextPageToken": "same"}))
    with pytest.raises(ValueError, match="Repeated"):
        scan.sync("s2", "2025-01-01", "2025-01-03")
    assert scan_status(scan.catalog.path)["collections"][0]["completed_windows"] == 0


def test_overlap_only_fetches_missing_dates(tmp_path):
    fetch = Mock(return_value={"images": []})
    scan = scanner(tmp_path, fetch)
    scan.sync("s2", "2025-01-02", "2025-01-04")
    fetch.reset_mock()
    scan.sync("s2", "2025-01-01", "2025-01-05", workers=1)
    assert [
        (c.args[0]["filter"].split(chr(34))[1][:10], c.args[0]["filter"].split(chr(34))[3][:10])
        for c in fetch.call_args_list
    ] == [("2025-01-01", "2025-01-02"), ("2025-01-04", "2025-01-05")]


def test_native_grids():
    assert (
        grid_key("LANDSAT/LT05/C02/T1_L2", {"properties": {"WRS_PATH": 1, "WRS_ROW": 53}})
        == "001/053"
    )
    assert (
        grid_key("NASA/HLS/HLSS30/v002", {"id": "NASA/HLS/HLSS30/v002/T01GDM_20260629T214749"})
        == "01GDM"
    )
    assert grid_key("NASA/VIIRS/002/VNP09GA", {}) == "global"


def test_retry_transport_but_not_permissions(monkeypatch):
    monkeypatch.setattr("hpgee.runtime.retry.time.sleep", lambda _: None)
    op = Mock(side_effect=[SSLError("EOF"), "ok"])
    assert retry_read(op) == "ok"
    op = Mock(side_effect=PermissionError("denied"))
    with pytest.raises(PermissionError):
        retry_read(op)
    assert op.call_count == 1


def test_spatial_lookup(tmp_path):
    scan = scanner(tmp_path, Mock(return_value={"images": [asset()]}))
    scan.sync("s2", "2025-01-01", "2025-01-02")
    assert scan.catalog.overlapping_tiles(COLLECTION, (0, 0, 1, 1)) == {"31NAA"}
    assert scan.catalog.overlapping_tiles(COLLECTION, (20, 20, 21, 21)) == set()


def test_global_complete_window_supplies_empty_tile_coverage(tmp_path):
    from hpgee.catalog.metadata import utc

    scan = scanner(tmp_path, Mock(return_value={"images": []}))
    scan.sync("s2", "2025-01-01", "2025-01-03")
    assert (
        scan.catalog.missing_intervals(COLLECTION, "32NAA", utc("2025-01-01"), utc("2025-01-03"))
        == []
    )
    assert scan.catalog.missing_intervals(
        COLLECTION, "32NAA", utc("2025-01-01"), utc("2025-01-03"), fetched_after=utc("2100-01-01")
    )


def test_filter_uses_acquisition_time_for_both_bounds(tmp_path):
    fetch = Mock(return_value={"images": []})
    scan = scanner(tmp_path, fetch)
    scan.sync("viirs", "2025-01-01", "2025-01-02")
    request = fetch.call_args.args[0]
    assert "startTime <" in request["filter"]
    assert "endTime" not in request


def test_sampler_refuses_silent_transport_pool_bottleneck(tmp_path):
    from dataclasses import replace
    from hpgee.sampling.config import SamplerConfig
    from hpgee.core.models import AuthConfig, RunConfig
    from hpgee.sampling.sampler import Sampler

    config = SamplerConfig(
        AuthConfig("project"), RunConfig(output_dir=tmp_path, workers=16), None, {}
    )
    sampler = Sampler(config, initialize=False)
    sampler.config = replace(config, run=replace(config.run, workers=32))
    with pytest.raises(ValueError, match="HTTP pool"):
        sampler._engine()


def test_wrapped_http_error_preserves_retryability():
    from hpgee.runtime.retry import is_transient

    inner = RuntimeError("HTTP failure")
    inner.resp = SimpleNamespace(status=503)
    outer = RuntimeError("SDK error")
    outer.__cause__ = inner
    assert is_transient(outer)
    inner.resp.status = 403
    assert not is_transient(outer)


def test_cyclic_exception_chain_is_not_infinitely_recursive():
    from hpgee.runtime.retry import is_transient

    first, second = RuntimeError("first"), RuntimeError("second")
    first.__cause__, second.__cause__ = second, first
    assert not is_transient(first)


def test_progress_does_not_count_cache_as_network(tmp_path):
    import json
    from hpgee.catalog.metadata import utc
    from hpgee.reporting.progress import summarize_metrics

    path = tmp_path / "metrics.jsonl"
    rows = [
        {"timestamp": "2025-01-01T00:10:00Z", "status": "success", "bytes_downloaded": 1048576},
        {"timestamp": "2025-01-01T00:11:00Z", "status": "skipped", "bytes_downloaded": 1048576},
        {"timestamp": "2025-01-01T00:30:00Z", "status": "success", "bytes_downloaded": 1048576},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    result = summarize_metrics([path, path], utc("2025-01-01"), utc("2025-01-01T00:30:00Z"))
    assert result["successful_image_bytes"] == 1048576
    assert result["status_counts"] == {"success": 1, "skipped": 1}
    assert result["image_mib_s"] == 1 / 1800


def test_gedi_index_normalization(tmp_path):
    from hpgee.catalog.metadata import scene_record, COLLECTIONS

    row = asset()
    row["id"] = "LARSE/GEDI/GEDI02_A_002/granule"
    result = scene_record(COLLECTIONS["gedi-index"], row)
    assert result.asset_id.endswith("/granule")


def test_concurrent_resume_keeps_original_windows(tmp_path):
    fetch = Mock(return_value={"images": []})
    scan = scanner(tmp_path, fetch)
    with scan.catalog._connect() as db:
        db.execute(
            "INSERT INTO metadata_scans(collection,start,end,updated_at) VALUES (?,?,?,?)",
            (COLLECTION, "2025-01-02T00:00:00+00:00", "2025-01-03T00:00:00+00:00", "2025-01-01"),
        )
    scan.sync("s2", "2025-01-01", "2025-01-05", chunk_days=1, workers=2)
    assert fetch.call_count == 4
    assert scan_status(scan.catalog.path)["collections"][0]["completed_windows"] == 4


def test_unbounded_viirs_mosaic_retains_source_geometry():
    from hpgee.catalog.metadata import scene_record, COLLECTIONS

    row = asset()
    row["id"] = COLLECTIONS["viirs"] + "/2025_01_01"
    row["geometry"] = {
        "type": "Polygon",
        "coordinates": [[["-Infinity", "-Infinity"], ["Infinity", "Infinity"]]],
    }
    row["properties"] = {}
    scene = scene_record(COLLECTIONS["viirs"], row)
    assert scene.bbox == (-180, -90, 180, 90)
    assert scene.properties["system:footprint"] == row["geometry"]
    assert "unbounded" in scene.properties["geesampler:footprint_kind"]


def test_second_scanner_cannot_write_while_locked(tmp_path):
    import sqlite3

    scan = scanner(tmp_path, Mock(return_value={"images": []}))
    lock = sqlite3.connect(str(scan.catalog.path) + ".scan-lock", timeout=0)
    try:
        lock.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            scan.sync("s2", "2025-01-01", "2025-01-02")
    finally:
        lock.close()
    assert scan.sync("s2", "2025-01-01", "2025-01-02")["complete"]
