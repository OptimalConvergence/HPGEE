import pytest
from hpgee.acquisition.target import observation_key, pack_cells


def test_observation_identity_keeps_distinct_orbits_and_sensors():
    a = {
        "scene": {"asset_id": "s1-a"},
        "metadata": {
            "properties": {"platform_number": "A", "orbitNumber_start": 1, "missionDataTakeID": 7}
        },
    }
    b = {
        "scene": {"asset_id": "s1-b"},
        "metadata": {
            "properties": {"platform_number": "A", "orbitNumber_start": 1, "missionDataTakeID": 7}
        },
    }
    assert observation_key(a, "S1") == observation_key(b, "S1")
    b["metadata"]["properties"]["orbitNumber_start"] = 2
    assert observation_key(a, "S1") != observation_key(b, "S1")
    assert observation_key(a, "S1") != observation_key(a, "L30")


def test_request_packing_preserves_exact_fixed_quality_cells():
    cells = [dict(row=0, col=0, id="a"), dict(row=0, col=3, id="b"), dict(row=4, col=4, id="c")]
    for size in (128, 256, 512, 1024):
        requests = pack_cells(cells, size)
        assert sorted(c["id"] for r in requests for c in r["cells"]) == ["a", "b", "c"]
        assert all(r["size"] == size for r in requests)
    with pytest.raises(ValueError):
        pack_cells(cells, 768)
