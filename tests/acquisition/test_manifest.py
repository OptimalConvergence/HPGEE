from hpgee.acquisition.manifest import freeze_task, requests_for_task, request_owner


def make_task():
    grid = {
        "crsCode": "EPSG:32648",
        "dimensions": {"width": 2048, "height": 128},
        "affineTransform": {"scaleX": 30, "scaleY": -30, "translateX": 0, "translateY": 0},
    }
    task = dict(
        region="region-1",
        observation="observation-1",
        sensor="S30",
        sources=[{"scene": {"asset_id": "A"}}],
        grid=grid,
    )
    rows = [
        dict(
            row=0,
            col=i,
            height=128,
            width=128,
            total=128**2,
            valid=128**2,
            cloud=0,
            land=128**2,
            keep=True,
        )
        for i in (0, 1, 7, 8, 15)
    ]
    rows.append(
        dict(
            row=0,
            col=2,
            height=128,
            width=128,
            total=128**2,
            valid=128**2,
            cloud=128**2,
            land=128**2,
            keep=False,
        )
    )
    return freeze_task(task, dict(rows=rows, key="quality-v1"))


def test_same_quality_manifest_is_delivered_for_every_transport_size():
    task = make_task()
    required = {c["id"] for c in task["cells"]}
    assert len(required) == 5
    for size in (128, 256, 512, 1024):
        requests = requests_for_task(task, size)
        assert {c["id"] for r in requests for c in r["cells"]} == required
        assert len([c for r in requests for c in r["cells"]]) == 5


def test_machine_assignment_does_not_change_when_pack_size_changes():
    task = make_task()
    owners = {}
    for size in (128, 256, 512, 1024):
        for request in requests_for_task(task, size):
            for cell in request["cells"]:
                expected = request_owner(request, 3)
                assert owners.setdefault(cell["id"], expected) == expected


def test_cache_identity_change_cannot_reuse_old_receipts():
    task = make_task()
    old = requests_for_task(task, 1024)
    task["quality_key"] = "different"
    new = requests_for_task(task, 1024)
    assert [r["id"] for r in old] != [r["id"] for r in new]
