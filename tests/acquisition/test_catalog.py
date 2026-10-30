from types import SimpleNamespace
from hpgee.acquisition.catalog import list_images, group_observations, ownership_predecessors


def test_full_metadata_is_paged_and_cached_with_start_time_boundaries(tmp_path):
    calls = []

    def query(params):
        calls.append(params)
        if not params.get("pageToken"):
            return {"images": [{"id": "A", "properties": {"v": 1}}], "nextPageToken": "next"}
        return {"images": [{"id": "B", "properties": {"v": 2}}]}

    ee = SimpleNamespace(data=SimpleNamespace(listImages=query))
    a = list_images(
        ee,
        "collection",
        "2018-01-01",
        "2018-04-01",
        {"type": "Point", "coordinates": [0, 0]},
        tmp_path,
    )
    assert [x["id"] for x in a] == ["A", "B"] and len(calls) == 2
    assert calls[0]["view"] == "FULL" and calls[0]["pageSize"] == 100
    assert 'startTime < "2018-04-01T00:00:00Z"' in calls[0]["filter"]
    assert "endTime" not in calls[0]["filter"]
    assert (
        list_images(
            ee,
            "collection",
            "2018-01-01",
            "2018-04-01",
            {"type": "Point", "coordinates": [0, 0]},
            tmp_path,
        )
        == a
    )
    assert len(calls) == 2
    list_images(
        ee,
        "collection",
        "2018-01-01",
        "2018-04-01",
        {"type": "Point", "coordinates": [0, 0]},
        tmp_path,
        cached=False,
    )
    assert len(calls) == 4


def test_same_observation_groups_slices_but_retains_other_orbits():
    def source(asset, orbit):
        return {
            "scene": {"asset_id": asset},
            "metadata": {
                "properties": {
                    "platform_number": "A",
                    "orbitNumber_start": orbit,
                    "missionDataTakeID": 7,
                }
            },
        }

    groups = group_observations(
        "r1", "S1", [source("slice-b", 1), source("slice-a", 1), source("other-orbit", 2)]
    )
    assert len(groups) == 2
    assert sorted(len(x["sources"]) for x in groups) == [1, 2]
    together = next(x for x in groups if len(x["sources"]) == 2)
    assert [s["scene"]["asset_id"] for s in together["sources"]] == ["slice-a", "slice-b"]


def test_overlap_ownership_only_subtracts_regions_requesting_the_same_observation():
    tasks = [
        dict(region="r1", sensor="S30", observation="date1"),
        dict(region="r2", sensor="S30", observation="date2"),
        dict(region="r2", sensor="S30", observation="date1"),
        dict(region="r3", sensor="L30", observation="date1"),
    ]
    result = ownership_predecessors(tasks)
    assert [x["overlap_predecessors"] for x in result] == [[], [], ["r1"], []]
