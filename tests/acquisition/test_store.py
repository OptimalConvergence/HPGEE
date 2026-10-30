import gzip
import pytest
from hpgee.acquisition.manifest import freeze_task, requests_for_task
from hpgee.acquisition.store import write_manifest, TaskStore


def task():
    grid = {
        "crsCode": "EPSG:32648",
        "dimensions": {"width": 256, "height": 150},
        "affineTransform": {"scaleX": 30, "scaleY": -30, "translateX": 0, "translateY": 0},
    }
    source = dict(scene={"asset_id": "frozen-asset"}, metadata={"version": 7, "large": "x" * 10000})
    value = dict(region="r1", sensor="S30", observation="o1", grid=grid, sources=[source])
    rows = [
        dict(
            row=0,
            col=0,
            height=128,
            width=128,
            total=128**2,
            valid=16384,
            cloud=123,
            shadow=0,
            land=16384,
            keep=True,
        ),
        dict(
            row=1,
            col=1,
            height=22,
            width=128,
            total=2816,
            valid=2000,
            cloud=0,
            shadow=0,
            land=2816,
            keep=True,
        ),
    ]
    return freeze_task(value, dict(key="frozen-quality", rows=rows))


def test_compact_stream_preserves_partial_cells_and_request_identity(tmp_path):
    value = task()
    header = dict(
        version=1,
        regions=[{"id": "r1", "grid": value["grid"], "land_pixels": 19200}],
        expected_task_ids=[value["id"]],
    )
    report = write_manifest(tmp_path / "target.jsonl.gz", header, iter([value]))
    with TaskStore(tmp_path / "target.jsonl.gz", tmp_path, report["id"]) as store:
        restored = store[value["id"]]
        assert restored["cells"] == value["cells"]
        for size in (128, 256, 512, 1024):
            assert requests_for_task(restored, size) == requests_for_task(value, size)
        assert store.summary["required_cells"] == 2
        assert store.summary["region_land_pixels"] == {"r1": 19200}
        assert "metadata" not in restored["sources"][0]
        assert restored["sources"][0]["metadata_sha256"]
        local = store.path
    assert not local.exists()
    raw = gzip.decompress((tmp_path / "target.jsonl.gz").read_bytes()).decode()
    assert "x" * 1000 not in raw


def test_modified_or_truncated_manifest_is_refused_before_any_download(tmp_path):
    value = task()
    p = tmp_path / "target.gz"
    report = write_manifest(
        p,
        dict(
            version=1, regions=[{"id": "r1", "land_pixels": 19200}], expected_task_ids=[value["id"]]
        ),
        [value],
    )
    raw = gzip.decompress(p.read_bytes()).replace(b"frozen-asset", b"changed-asset")
    p.write_bytes(gzip.compress(raw))
    with pytest.raises(ValueError, match="digest"):
        with TaskStore(p, tmp_path, report["id"]):
            pass
    assert not list(tmp_path.glob("task-stage-*"))


def test_nominal_land_area_is_not_summed_again_for_each_observation(tmp_path):
    a = task()
    b = dict(a, observation="o2")
    b = freeze_task(b, dict(key="q2", rows=a["cells"]))
    report = write_manifest(
        tmp_path / "target.gz",
        dict(
            version=1,
            regions=[{"id": "r1", "land_pixels": 19200}],
            expected_task_ids=[a["id"], b["id"]],
        ),
        [a, b],
    )
    assert report["required_cells"] == 4 and report["target_area_km2"] == pytest.approx(
        19200 * 0.0009
    )


def test_duplicate_task_or_inconsistent_region_area_cannot_publish_manifest(tmp_path):
    value = task()
    p = tmp_path / "target.gz"
    with pytest.raises(ValueError, match="Duplicate"):
        write_manifest(
            p,
            dict(
                version=1,
                regions=[{"id": "r1", "land_pixels": 19200}],
                expected_task_ids=[value["id"]],
            ),
            [value, value],
        )
    assert not p.exists()


def test_region_area_is_fixed_even_when_observation_specific_overlap_changes(tmp_path):
    a = task()
    b = dict(a, observation="different-date", owner_geometry={"type": "Polygon", "coordinates": []})
    rows = [dict(r) for r in a["cells"]]
    rows[0].update(valid=8192, land=8192)
    b = freeze_task(b, dict(key="owned-q", rows=rows))
    header = dict(
        version=1, regions=[dict(id="r1", land_pixels=19200)], expected_task_ids=[a["id"], b["id"]]
    )
    report = write_manifest(tmp_path / "target.gz", header, [a, b])
    assert report["region_land_pixels"] == {"r1": 19200}


def test_missing_preparation_cannot_be_published_as_a_smaller_complete_target(tmp_path):
    a = task()
    header = dict(
        version=1,
        regions=[dict(id="r1", land_pixels=19200)],
        expected_task_ids=[a["id"], "missing-observation"],
    )
    with pytest.raises(ValueError, match="Missing"):
        write_manifest(tmp_path / "target.gz", header, [a])
    assert not (tmp_path / "target.gz").exists()
