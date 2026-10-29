from __future__ import annotations

import json
import logging
import math
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import EECUMonitorConfig

LOGGER = logging.getLogger(__name__)
COMPLETED_METRIC = "earthengine.googleapis.com/project/cpu/usage_time"
IN_PROGRESS_METRIC = "earthengine.googleapis.com/project/cpu/in_progress_usage_time"


@dataclass(frozen=True)
class EECUSnapshot:
    timestamp: str
    completed_seconds: float | None
    in_progress_seconds: float | None
    available: bool = True
    error: str | None = None

    @property
    def guard_seconds(self) -> float:
        """Best available usage for guarding without double-counting both metrics."""
        values = [v for v in (self.completed_seconds, self.in_progress_seconds) if v is not None]
        return max(values) if values else None


def _point_value(point: Any) -> float:
    value = point.value
    name = value._pb.WhichOneof("value")
    if name not in ("double_value", "int64_value"):
        raise ValueError("Unsupported Monitoring value type")
    result = float(getattr(value, name))
    if not math.isfinite(result) or result < 0:
        raise ValueError("Invalid Monitoring usage value")
    return result


class MonitoringRESTClient:
    """Official timeSeries REST with explicit EE credentials and bounded pages.

    Older google-cloud-monitoring releases only provide gRPC transports; REST
    here uses the same HTTPS proxy path as pixel/auth requests.
    """
    def __init__(self, credentials=None, *, session=None):
        if session is None:
            from google.auth.transport.requests import AuthorizedSession
            session=AuthorizedSession(credentials)
        self.session=session
        self.http_attempts=0

    def list_time_series(self, *, request, timeout=30, retry=None):
        from google.cloud import monitoring_v3
        interval=request['interval']
        params={'filter':request['filter'], 'interval.startTime':interval.start_time.isoformat(),
                'interval.endTime':interval.end_time.isoformat(), 'view':'FULL','pageSize':1000}
        seen=set()
        for _ in range(10000):
            self.http_attempts+=1
            response=self.session.get('https://monitoring.googleapis.com/v3/'+request['name']+'/timeSeries',
                                      params=params,timeout=timeout)
            response.raise_for_status();data=response.json()
            for item in data.get('timeSeries',[]):
                yield monitoring_v3.TimeSeries.from_json(json.dumps(item))
            token=data.get('nextPageToken')
            if not token:return
            if token in seen:raise ValueError('Repeated Monitoring page token')
            seen.add(token);params['pageToken']=token
        raise RuntimeError('Monitoring pagination cap reached')


class CloudEECUReader:
    """Fixed-start cumulative DELTA usage, same EE identity, full pager iteration.

    Missing points remain unknown. Re-querying replaces, never adds snapshots.
    lookback_minutes is retained for source compatibility, fixed at construction;
    engines pass their durable run start explicitly.
    """
    def __init__(self, project, workload_tag, lookback_minutes=30, *, start=None,
                 credentials=None, client=None, tag_prefix=False):
        from google.cloud import monitoring_v3
        if not re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]', project):
            raise ValueError('Invalid project')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', workload_tag):
            raise ValueError('Invalid workload tag')
        self.project, self.workload_tag = project, workload_tag
        self.tag_prefix = tag_prefix
        self.start = start or datetime.now(timezone.utc)-timedelta(minutes=lookback_minutes)
        if isinstance(self.start,str): self.start=datetime.fromisoformat(self.start.replace('Z','+00:00'))
        if self.start.tzinfo is None: raise ValueError('UTC offset required')
        self._monitoring_v3 = monitoring_v3
        if client is None:
            if credentials is None:
                import ee
                state = ee.data._get_state() if hasattr(ee.data, '_get_state') else ee.data
                credentials = getattr(state, 'credentials', getattr(state, '_credentials', None))
                if credentials is None: credentials=ee.data.get_persistent_credentials()
            client=MonitoringRESTClient(credentials=credentials)
        self._client=client
        self.raw_points=[]

    def _sum(self, metric_type, now):
        interval=self._monitoring_v3.TimeInterval({'start_time':self.start,'end_time':now})
        query=(f'metric.type = "{metric_type}" AND resource.labels.project_id = "{self.project}" '
               f'AND metric.labels.workload_tag = "{self.workload_tag}"')
        if self.tag_prefix:
            query = (f'metric.type = "{metric_type}" AND resource.labels.project_id = "{self.project}" '
                     f'AND metric.labels.workload_tag = monitoring.regex.full_match("{self.workload_tag}.*")')
        series=self._client.list_time_series(request={'name':f'projects/{self.project}',
            'filter':query,'interval':interval,
            'view':self._monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL},timeout=30,retry=None)
        points={}
        for item in series:
            tag=item.metric.labels.get('workload_tag','')
            tag_ok=tag.startswith(self.workload_tag) if self.tag_prefix else tag==self.workload_tag
            if item.resource.labels.get('project_id')!=self.project or not tag_ok:
                raise ValueError('Monitoring project/tag mismatch')
            for point in item.points:
                identity=dict(metric=metric_type,resource_type=item.resource.type,resource=dict(item.resource.labels),
                              labels=dict(item.metric.labels),start=str(point.interval.start_time),end=str(point.interval.end_time))
                points[json.dumps(identity,sort_keys=True)]=dict(identity,value=_point_value(point))
        self.raw_points.extend(points.values())
        return sum(p['value'] for p in points.values()) if points else None

    def read(self):
        now=datetime.now(timezone.utc);self.raw_points=[]
        completed=self._sum(COMPLETED_METRIC,now);progress=self._sum(IN_PROGRESS_METRIC,now)
        return EECUSnapshot(now.isoformat(),completed,progress,
                            available=completed is not None or progress is not None)


class EECUMonitor:
    def __init__(
        self,
        config: EECUMonitorConfig,
        reader: CloudEECUReader | None,
        metrics_path: Path,
        completed_samples: Callable[[], int],
    ):
        self.config = config
        self.reader = reader
        self.metrics_path = metrics_path
        self.completed_samples = completed_samples
        self.latest: EECUSnapshot | None = None
        self.unavailable_error: str | None = None
        self.warning_reached = False
        self.hard_limit_reached = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()

    def start(self) -> None:
        if not self.config.enabled:
            return
        if self.reader is None:
            self.unavailable_error = "Cloud Monitoring reader is unavailable"
            if self.config.required:
                raise RuntimeError(self.unavailable_error)
            LOGGER.warning("EECU monitoring unavailable; continuing without an EECU guard")
            return
        self._thread = threading.Thread(target=self._loop, name="eecu-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.config.poll_seconds + 1.0))

    def poll_once(self) -> EECUSnapshot | None:
        if self.reader is None:
            return None
        try:
            snapshot = self.reader.read()
            self.latest = snapshot
            hours = snapshot.guard_seconds / 3600.0 if snapshot.guard_seconds is not None else None
            if hours is None:
                if self.config.required: self.hard_limit_reached = True
                self._append(snapshot)
                return snapshot
            if (
                self.config.warning_eecu_hours is not None
                and hours >= self.config.warning_eecu_hours
            ):
                if not self.warning_reached:
                    LOGGER.warning("EECU warning threshold reached: %.4f EECU-hours", hours)
                self.warning_reached = True
            if self.config.hard_eecu_hours is not None and hours >= self.config.hard_eecu_hours:
                if not self.hard_limit_reached:
                    LOGGER.warning("EECU scheduling ceiling reached: %.4f EECU-hours", hours)
                self.hard_limit_reached = True
            self._append(snapshot)
            return snapshot
        except Exception as exc:  # noqa: BLE001 - optional telemetry must not crash downloads
            self.unavailable_error = type(exc).__name__
            if self.config.required:
                self.hard_limit_reached = True
            self._append(EECUSnapshot(datetime.now(timezone.utc).isoformat(),None,None,False,type(exc).__name__))
            LOGGER.warning("Unable to read EECU metrics (%s)", type(exc).__name__)
            return None

    def _append(self, snapshot: EECUSnapshot) -> None:
        completed = self.completed_samples()
        payload = asdict(snapshot)
        payload["raw_points"] = getattr(self.reader, "raw_points", [])
        payload["telemetry_complete"] = False
        payload["completed_samples"] = completed
        payload["eecu_per_success"] = snapshot.completed_seconds / completed if completed and snapshot.completed_seconds is not None else None
        payload["projected_cost"] = (
            snapshot.completed_seconds / 3600.0 * self.config.price_per_eecu_hour
            if self.config.price_per_eecu_hour is not None and snapshot.completed_seconds is not None
            else None
        )
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self.metrics_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.config.poll_seconds)


class SequenceEECUReader:
    """Deterministic reader used by tests and offline demonstrations."""

    def __init__(self, snapshots: Iterable[EECUSnapshot]):
        self._snapshots = iter(snapshots)
        self._last: EECUSnapshot | None = None

    def read(self) -> EECUSnapshot:
        try:
            self._last = next(self._snapshots)
        except StopIteration:
            if self._last is None:
                raise RuntimeError("No EECU snapshots configured")
        return self._last
