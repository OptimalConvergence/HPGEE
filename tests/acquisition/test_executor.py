import json
from types import SimpleNamespace
import numpy as np
from hpgee.acquisition.executor import CredentialHeaders, execute
import pytest
from hpgee.acquisition.manifest import freeze_task
from hpgee.acquisition.store import write_manifest, TaskStore
from acquisition.test_delivery import tiff_bytes


def test_fixed_task_deferred_recovery_then_resume_without_network(tmp_path):
    grid = {
        "crsCode": "EPSG:32648",
        "dimensions": {"width": 256, "height": 128},
        "affineTransform": {
            "scaleX": 30,
            "scaleY": -30,
            "translateX": 500000,
            "translateY": 3000000,
        },
    }
    rows = [
        dict(
            row=0,
            col=x,
            height=128,
            width=128,
            total=16384,
            valid=16384,
            cloud=0,
            land=16384,
            keep=True,
        )
        for x in (0, 1)
    ]
    task = freeze_task(
        dict(
            region="r",
            observation="o",
            sensor="S30",
            grid=grid,
            sources=[{"scene": {"asset_id": "A"}}],
        ),
        dict(key="q", rows=rows),
    )
    path = tmp_path / "manifest.gz"
    report = write_manifest(
        path,
        dict(version=1, regions=[dict(id="r", land_pixels=32768)], expected_task_ids=[task["id"]]),
        [task],
    )
    attempts = []
    first = [True]

    class HTTP:
        def request(self, url, **kwargs):
            request = json.loads(kwargs["body"])
            attempts.append(request["id"])
            if first[0]:
                first[0] = False
                raise TimeoutError("transient")
            values = np.zeros((8, 128, 128), dtype="int16")
            values[7] = 127
            return SimpleNamespace(status=200), tiff_bytes(values, request["grid"])

    credentials = SimpleNamespace(
        valid=True, apply=lambda h: h.update(Authorization="private-test-token")
    )
    options = dict(
        ee=None,
        http=HTTP(),
        credentials=credentials,
        project="test",
        endpoint="standard",
        tile=128,
        request_mode="eager",
        workers=2,
        rank=0,
        hosts=1,
        output=tmp_path / "output",
        scratch=tmp_path,
        retry_rounds=1,
        make_body=lambda ee, t, r, tag: json.dumps(r),
    )
    with TaskStore(path, tmp_path, report["id"]) as store:
        result = execute(store, **options)
        assert (
            result["complete"] and result["completed_cells"] == 2 and result["failed_requests"] == 0
        )
        assert len(attempts) == 3 and result["successful_requests"] == 2
        assert result["seconds_per_km2"] is not None
    with TaskStore(path, tmp_path, report["id"]) as store:
        again = execute(store, **options)
        assert again["complete"] and again["skipped_requests"] == 2 and len(attempts) == 3
    assert not list(tmp_path.glob("eager-*"))


def test_stale_401_does_not_discard_a_new_token():
    credentials = SimpleNamespace(valid=True, token="old")
    credentials.apply = lambda headers: headers.update(Authorization="Bearer " + credentials.token)
    auth = CredentialHeaders(credentials, None)
    old = auth.get()
    credentials.token = "new"
    current = auth.get()
    auth.invalidate(old)
    assert credentials.token == "new"
    auth.invalidate(current)
    assert credentials.token is None


@pytest.mark.parametrize("hosts,rank", [(0, 0), (3, -1), (3, 3)])
def test_invalid_shard_rejected_before_work(tmp_path, hosts, rank):
    with pytest.raises(ValueError):
        execute(
            None,
            ee=None,
            http=None,
            credentials=None,
            project="test",
            endpoint="standard",
            tile=128,
            request_mode="lazy",
            workers=1,
            rank=rank,
            hosts=hosts,
            output=tmp_path / "out",
            scratch=tmp_path,
        )
    assert not (tmp_path / "out").exists()


def test_byte_limit_stops_admission_and_is_not_reported_as_failure_or_recovered(tmp_path, monkeypatch):
    import hpgee.acquisition.executor as executor

    grid = {
        "crsCode": "EPSG:32648",
        "dimensions": {"width": 384, "height": 128},
        "affineTransform": {"scaleX": 30, "scaleY": -30, "translateX": 500000, "translateY": 3000000},
    }
    rows = [
        dict(row=0, col=x, height=128, width=128, total=16384, valid=16384, cloud=0, land=16384, keep=True)
        for x in (0, 1, 2)
    ]
    task = freeze_task(
        dict(region="r", observation="o", sensor="S30", grid=grid, sources=[{"scene": {"asset_id": "A"}}]),
        dict(key="q", rows=rows),
    )
    path = tmp_path / "manifest.gz"
    report = write_manifest(
        path, dict(version=1, regions=[dict(id="r", land_pixels=None)], expected_task_ids=[task["id"]]), [task]
    )
    sent = []
    monkeypatch.setattr(executor, "verified", lambda request, destination: None)
    monkeypatch.setattr(
        executor, "publish", lambda payload, request, destination: dict(cells=[c["id"] for c in request["cells"]])
    )

    class HTTP:
        def request(self, url, **kwargs):
            sent.append(json.loads(kwargs["body"])["id"])
            return SimpleNamespace(status=200), b"payload"

    credentials = SimpleNamespace(valid=True, apply=lambda h: h.update(Authorization="t"))
    with TaskStore(path, tmp_path, report["id"]) as store:
        result = executor.execute(
            store, ee=None, http=HTTP(), credentials=credentials, project="test",
            endpoint="standard", tile=128, request_mode="lazy", workers=1, rank=0, hosts=1,
            output=tmp_path / "output", scratch=tmp_path, retry_rounds=1, max_bytes=1,
            make_body=lambda ee, t, r, tag: json.dumps(r),
        )
    assert len(sent) == 1 and result["failed_attempts"] == 0
    assert result["failed_requests"] == 2 and not result["complete"]
    assert result["sampling_status"] == "storage_limited"
