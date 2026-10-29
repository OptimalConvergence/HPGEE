from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hpgee.accounting import metrics
from hpgee.core.models import EECUMonitorConfig

LOGGER = logging.getLogger(__name__)
COMPLETED_METRIC = metrics.COMPLETED_METRIC
IN_PROGRESS_METRIC = metrics.IN_PROGRESS_METRIC
MonitoringRESTClient = metrics.MonitoringRESTClient


@dataclass(frozen=True)
class EECUSnapshot:
    timestamp: str
    completed_seconds: float | None
    in_progress_seconds: float | None
    available: bool = True
    error: str | None = None

    @property
    def guard_seconds(self) -> float | None:
        """Best available usage for guarding without double-counting both metrics."""
        return metrics.guard_seconds(self.completed_seconds, self.in_progress_seconds)


class CloudEECUReader:
    """Fixed-start cumulative DELTA usage, same EE identity, full pager iteration.

    Missing points remain unknown. Re-querying replaces, never adds snapshots.
    lookback_minutes is retained for source compatibility, fixed at construction;
    engines pass their durable run start explicitly.
    """
    def __init__(self, project, workload_tag, lookback_minutes=30, *, start=None,
                 credentials=None, client=None, tag_prefix=False):
        self.project, self.workload_tag = project, workload_tag
        self.tag_prefix = tag_prefix
        self.start = metrics.utc_datetime(start or datetime.now(timezone.utc)-timedelta(minutes=lookback_minutes))
        metrics.validate_scope(project, workload_tag, tag_prefix=tag_prefix)
        if client is None:
            client = MonitoringRESTClient(credentials=credentials)
        self._reader = metrics.MonitoringReader(project, workload_tag, credentials=credentials,
                                                client=client, tag_prefix=tag_prefix)
        self._client = self._reader.client
        self.raw_points=[]

    def read(self):
        now=datetime.now(timezone.utc);self.raw_points=[]
        try:
            observation = self._reader.read(start=self.start, end=now)
        finally:
            # Preserve the sampler log format, including partial raw evidence.
            self.raw_points = [dict(point, resource_type=point['resource']['type'],
                                    resource=point['resource']['labels']) for point in self._reader.raw_points]
        return EECUSnapshot(now.isoformat(),observation.completed_seconds,observation.in_progress_seconds,
                            available=observation.completed_seconds is not None or observation.in_progress_seconds is not None)


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
