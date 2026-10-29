"""Offline regression tests for the canonical GEESampler Monitoring reader."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from google.cloud import monitoring_v3 as monitoring_v3

from geesampler.models import EECUMonitorConfig
from geesampler.monitoring import (
    COMPLETED_METRIC,
    IN_PROGRESS_METRIC,
    CloudEECUReader,
    EECUMonitor,
    MonitoringRESTClient,
)


PROJECT = "project-x"
TAG = "download-run-2026-10-25"
START = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)


class FrozenClock:
    current: datetime

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


def _point_time(value: datetime) -> dict[str, int]:
    return {"seconds": int(value.timestamp())}


def _series(metric: str, value: int | float, start: datetime, end: datetime,
            *, project: str = PROJECT, tag: str = TAG):
    value_field = "int64_value" if isinstance(value, int) else "double_value"
    return monitoring_v3.TimeSeries({
        "metric": {"type": metric, "labels": {"workload_tag": tag}},
        "resource": {"type": "earthengine_project", "labels": {"project_id": project}},
        "points": [{
            "interval": {"start_time": _point_time(start), "end_time": _point_time(end)},
            "value": {value_field: value},
        }],
    })


def _json_series(value) -> dict:
    return json.loads(monitoring_v3.TimeSeries.to_json(value))


class BoundaryClient:
    """Supplies cumulative Monitoring pages while preserving real reader behavior."""

    def __init__(self):
        a = _series(COMPLETED_METRIC, 100, START, START + timedelta(minutes=10))
        late_old = _series(COMPLETED_METRIC, 35, START + timedelta(minutes=10),
                           START + timedelta(minutes=20))
        b = _series(COMPLETED_METRIC, 65.0, START + timedelta(minutes=20),
                    START + timedelta(minutes=31))
        c = _series(COMPLETED_METRIC, 90, START + timedelta(minutes=31),
                    START + timedelta(minutes=65))
        self.completed = (
            [a, a],
            [a, a, late_old, b],
            [a, late_old, b, c],
        )
        self.in_progress = (
            [_series(IN_PROGRESS_METRIC, 40.0, START, START + timedelta(minutes=29))],
            [_series(IN_PROGRESS_METRIC, 225.0, START, START + timedelta(minutes=31))],
            [_series(IN_PROGRESS_METRIC, 20, START, START + timedelta(minutes=65))],
        )
        self.poll_index = 0
        self.requests = []

    def list_time_series(self, *, request, **kwargs):
        self.requests.append(request)
        if IN_PROGRESS_METRIC in request["filter"]:
            rows = self.in_progress[self.poll_index]
            self.poll_index += 1
            return iter(rows)
        return iter(self.completed[self.poll_index])


def test_fixed_start_cumulative_prefix_survives_30_minute_boundary_and_late_point(tmp_path):
    persisted_path = tmp_path / "run-state.json"
    persisted_path.write_text(json.dumps({"run_started_at": "2026-10-25T18:00:00+00:00"}))
    persisted_state = json.loads(persisted_path.read_text())
    run_start = datetime.fromisoformat(persisted_state["run_started_at"])
    client = BoundaryClient()
    reader = CloudEECUReader(PROJECT, TAG, start=run_start, client=client)
    monitor = EECUMonitor(
        EECUMonitorConfig(enabled=True, required=False, poll_seconds=30),
        reader,
        tmp_path / "eecu.jsonl",
        lambda: 12,
    )

    elapsed_minutes = (29, 31, 65)
    expected_ends = [START + timedelta(minutes=minute) for minute in elapsed_minutes]
    snapshots = []
    with patch("geesampler.monitoring.datetime", FrozenClock):
        for current in expected_ends:
            FrozenClock.current = current
            snapshots.append(monitor.poll_once())

    assert reader.start == START
    assert len(client.requests) == 6
    assert [request["interval"].start_time for request in client.requests] == [START] * 6
    assert [request["interval"].end_time for request in client.requests] == [
        end for end in expected_ends for _ in range(2)
    ]

    completed = [snapshot.completed_seconds for snapshot in snapshots]
    assert completed == [100.0, 200.0, 290.0]
    assert completed[0] <= completed[1] <= completed[2]
    # The 35-second interval ending at minute 20 is first posted at the 31-minute poll.
    assert sum(p["value"] for p in reader.raw_points if p["metric"] == COMPLETED_METRIC) == 290.0
    assert [snapshot.in_progress_seconds for snapshot in snapshots] == [40.0, 225.0, 20.0]
    assert [snapshot.guard_seconds for snapshot in snapshots] == [100.0, 225.0, 290.0]
    assert snapshots[1].guard_seconds != snapshots[1].completed_seconds + snapshots[1].in_progress_seconds

    history = [json.loads(line) for line in (tmp_path / "eecu.jsonl").read_text().splitlines()]
    assert len(history) == 3
    assert [row["completed_seconds"] for row in history] == [100.0, 200.0, 290.0]
    assert [row["timestamp"] for row in history] == [end.isoformat() for end in expected_ends]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class PagedSession:
    def __init__(self):
        self.calls = []
        self.first = _series(COMPLETED_METRIC, 10, START, START + timedelta(minutes=10))
        self.late = _series(COMPLETED_METRIC, 5.5, START + timedelta(minutes=10),
                            START + timedelta(minutes=20))

    def get(self, url, *, params, timeout):
        self.calls.append((url, dict(params), timeout))
        assert url == f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/timeSeries"
        assert timeout == 30
        if IN_PROGRESS_METRIC in params["filter"]:
            return FakeResponse({})
        if "pageToken" not in params:
            return FakeResponse({"timeSeries": [_json_series(self.first)], "nextPageToken": "p2"})
        assert params["pageToken"] == "p2"
        return FakeResponse({"timeSeries": [_json_series(self.first), _json_series(self.late)]})


def test_rest_pages_keep_int_and_float_values_deduplicate_and_leave_empty_metric_unknown():
    session = PagedSession()
    client = MonitoringRESTClient(session=session)
    reader = CloudEECUReader(PROJECT, TAG, start=START, client=client)
    FrozenClock.current = START + timedelta(minutes=65)
    with patch("geesampler.monitoring.datetime", FrozenClock):
        snapshot = reader.read()

    assert snapshot.completed_seconds == 15.5
    assert snapshot.in_progress_seconds is None
    assert snapshot.guard_seconds == 15.5
    assert snapshot.available is True
    assert len(reader.raw_points) == 2
    assert client.http_attempts == 3
    assert [call[1].get("pageToken") for call in session.calls] == [None, "p2", None]
    assert all(call[1]["interval.startTime"] == START.isoformat() for call in session.calls)


def test_empty_monitoring_response_is_unknown_not_zero():
    class EmptyClient:
        def list_time_series(self, **kwargs):
            return iter(())

    reader = CloudEECUReader(PROJECT, TAG, start=START, client=EmptyClient())
    FrozenClock.current = START + timedelta(minutes=65)
    with patch("geesampler.monitoring.datetime", FrozenClock):
        snapshot = reader.read()

    assert snapshot.completed_seconds is None
    assert snapshot.in_progress_seconds is None
    assert snapshot.guard_seconds is None
    assert snapshot.available is False


def test_monitoring_timeout_is_recorded_as_unavailable_not_zero(tmp_path):
    class FailingSession:
        def get(self, url, *, params, timeout):
            raise TimeoutError("synthetic offline timeout")

    reader = CloudEECUReader(
        PROJECT, TAG, start=START,
        client=MonitoringRESTClient(session=FailingSession()),
    )
    monitor = EECUMonitor(
        EECUMonitorConfig(enabled=True, required=False, poll_seconds=30),
        reader,
        tmp_path / "fault.jsonl",
        lambda: 0,
    )
    FrozenClock.current = START + timedelta(minutes=65)
    with patch("geesampler.monitoring.datetime", FrozenClock):
        result = monitor.poll_once()

    assert result is None
    assert monitor.latest is None
    assert monitor.unavailable_error == "TimeoutError"
    row = json.loads((tmp_path / "fault.jsonl").read_text())
    assert row["available"] is False
    assert row["error"] == "TimeoutError"
    assert row["completed_seconds"] is None
    assert row["in_progress_seconds"] is None
    assert row["eecu_per_success"] is None


@pytest.mark.parametrize(
    ("project", "tag"),
    [("project-y", TAG), (PROJECT, "other-run")],
)
def test_reader_rejects_wrong_monitoring_project_or_workload_tag(project, tag):
    class MismatchedClient:
        def list_time_series(self, *, request, **kwargs):
            return iter([_series(COMPLETED_METRIC, 1, START, START + timedelta(minutes=1),
                                 project=project, tag=tag)])

    reader = CloudEECUReader(PROJECT, TAG, start=START, client=MismatchedClient())
    FrozenClock.current = START + timedelta(minutes=65)
    with patch("geesampler.monitoring.datetime", FrozenClock):
        with pytest.raises(ValueError, match="project/tag mismatch"):
            reader.read()
