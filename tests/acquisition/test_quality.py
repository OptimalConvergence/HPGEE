import numpy as np
import pytest

from hpgee.acquisition.quality import decide, summarize, approximate, compare_decisions, cache_key


def test_quality_boundaries_include_clouds_in_supported_denominator():
    assert not decide(157286, 0, 1024**2, "S30")["keep"]
    assert decide(157287, 0, 1024**2, "L30")["keep"]
    assert decide(100, 80, 100, "S30")["keep"]
    assert not decide(100, 81, 100, "S30")["keep"]
    assert decide(100, 100, 100, "S1")["keep"]
    assert not decide(0, 0, 100, "S1")["keep"]


def test_invalid_cloud_bits_and_partial_edges_do_not_count_as_clouds():
    valid = np.ones((129, 130), dtype=bool)
    valid[:128, :128] = False
    cloud = np.ones_like(valid)
    rows = summarize(valid, cloud, 128, "S30")
    assert [(r["height"], r["width"]) for r in rows] == [(128, 128), (128, 2), (1, 128), (1, 2)]
    assert rows[0]["valid"] == 0 and rows[0]["cloud"] == 0
    assert sum(r["total"] for r in rows) == 129 * 130


def test_coarse_statistics_are_labeled_and_measured_against_truth():
    valid = np.ones((128, 128), dtype=bool)
    cloud = np.zeros_like(valid)
    exact = summarize(valid, cloud, 128, "S30")
    for scale in (100, 200):
        estimate = approximate(valid, cloud, 128, "S30", scale)
        report = compare_decisions(exact, estimate)
        assert report["false_rejects"] == 0 and report["false_accepts"] == 0
        assert report["cells"] == 1
        assert estimate[0]["scale"] == scale


def test_quality_cache_identity_includes_source_grid_and_policy():
    common = dict(sources=["A"], grid={"origin": 1}, sensor="L30", scale=30)
    original = cache_key(**common)
    for key, value in [
        ("sources", ["B"]),
        ("grid", {"origin": 2}),
        ("sensor", "S30"),
        ("scale", 100),
    ]:
        assert original != cache_key(**dict(common, **{key: value}))


@pytest.mark.parametrize(
    "valid,cloud,total", [(101, 0, 100), (10, 11, 100), (-1, 0, 100), (0, 0, 0)]
)
def test_bad_counts_refused(valid, cloud, total):
    with pytest.raises(ValueError):
        decide(valid, cloud, total, "S30")
