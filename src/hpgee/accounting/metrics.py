"""Common Earth Engine usage queries and provider-point accounting.

Completed and in-progress DELTA metrics are overlapping views. Missing telemetry
stays unknown, and repeated provider points replace earlier observations.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from hpgee.runtime.auth import resolve_earth_engine_credentials

COMPLETED_METRIC = 'earthengine.googleapis.com/project/cpu/usage_time'
IN_PROGRESS_METRIC = 'earthengine.googleapis.com/project/cpu/in_progress_usage_time'


def utc_datetime(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Usage interval requires an explicit UTC offset')
    return result.astimezone(timezone.utc)


def validate_scope(project, workload_tag=None, *, tag_prefix=False):
    if not isinstance(project, str) or not re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]', project):
        raise ValueError('Invalid project ID')
    if workload_tag is not None and (not isinstance(workload_tag, str) or
            not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', workload_tag)):
        raise ValueError('Invalid workload tag')
    if tag_prefix and workload_tag is None:
        raise ValueError('A workload tag prefix is required')


def guard_seconds(completed_seconds, in_progress_seconds):
    """Use the larger overlapping view, never their sum or an invented zero."""
    values = [value for value in (completed_seconds, in_progress_seconds) if value is not None]
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError('Usage values must be finite and nonnegative')
    return max(values) if values else None


def sum_usage(values):
    values = list(values)
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError('Usage values must be finite and nonnegative')
    return sum(values) if values else None


def _point_identity(point):
    resource = point['resource']
    if 'resource_type' in point:
        resource = {'type': point['resource_type'], 'labels': resource}
    identity = {'metric': point['metric'], 'resource': resource, 'labels': point['labels']}
    for key in ('start', 'end'):
        value = point.get(key)
        identity[key] = utc_datetime(value).isoformat() if value not in (None, 'None') else None
    return json.dumps(identity, sort_keys=True)


def deduplicate_points(points):
    """Reconcile both persisted point formats while preserving the latest row."""
    result = {}
    for point in points:
        result[_point_identity(point)] = point
    return list(result.values())


def _point_value(point):
    name = point.value._pb.WhichOneof('value')
    if name not in ('double_value', 'int64_value'):
        raise ValueError('Unsupported Monitoring value type')
    value = float(getattr(point.value, name))
    if not math.isfinite(value) or value < 0:
        raise ValueError('Invalid Monitoring usage value')
    return value


class MonitoringRESTClient:
    """Paginated Monitoring REST over the same HTTPS proxy route as EE requests."""
    def __init__(self, credentials=None, *, session=None):
        if session is None:
            from google.auth.transport.requests import AuthorizedSession
            session = AuthorizedSession(resolve_earth_engine_credentials(credentials))
        self.session = session
        self.http_attempts = 0

    def list_time_series(self, *, request, timeout=30, retry=None):
        from google.cloud import monitoring_v3
        interval = request['interval']
        params = {'filter': request['filter'], 'interval.startTime': interval.start_time.isoformat(),
                  'interval.endTime': interval.end_time.isoformat(), 'view': 'FULL', 'pageSize': 1000}
        seen = set()
        for _ in range(10000):
            self.http_attempts += 1
            response = self.session.get('https://monitoring.googleapis.com/v3/' + request['name'] + '/timeSeries',
                                        params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            for item in data.get('timeSeries', []):
                yield monitoring_v3.TimeSeries.from_json(json.dumps(item))
            token = data.get('nextPageToken')
            if not token:
                return
            if token in seen:
                raise ValueError('Repeated Monitoring page token')
            seen.add(token)
            params['pageToken'] = token
        raise RuntimeError('Monitoring pagination cap reached')


@dataclass(frozen=True)
class UsageObservation:
    completed_seconds: float | None
    in_progress_seconds: float | None
    raw_points: list[dict]


class MonitoringReader:
    """One fixed-interval reader for cumulative, stage, and project usage views."""
    def __init__(self, project, workload_tag=None, *, credentials=None, client=None, tag_prefix=False):
        validate_scope(project, workload_tag, tag_prefix=tag_prefix)
        self.project, self.workload_tag, self.tag_prefix = project, workload_tag, tag_prefix
        self.client = client if client is not None else MonitoringRESTClient(credentials=credentials)
        self.raw_points = []

    def read(self, *, start, end):
        from google.cloud import monitoring_v3
        self.raw_points = []
        start, end = utc_datetime(start), utc_datetime(end)
        if start >= end:
            raise ValueError('Usage interval must have start < end')
        interval = monitoring_v3.TimeInterval({'start_time': start, 'end_time': end})
        raw_points = []
        for metric in (COMPLETED_METRIC, IN_PROGRESS_METRIC):
            query = f'metric.type = "{metric}" AND resource.labels.project_id = "{self.project}"'
            if self.workload_tag is not None:
                scope = (f'monitoring.regex.full_match("{self.workload_tag}.*")' if self.tag_prefix
                         else f'"{self.workload_tag}"')
                query += f' AND metric.labels.workload_tag = {scope}'
            series = self.client.list_time_series(request={
                'name': f'projects/{self.project}', 'filter': query, 'interval': interval,
                'view': monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }, timeout=30, retry=None)
            for item in series:
                tag = item.metric.labels.get('workload_tag', '')
                tag_ok = (self.workload_tag is None or
                          (tag.startswith(self.workload_tag) if self.tag_prefix else tag == self.workload_tag))
                if item.resource.labels.get('project_id') != self.project or not tag_ok:
                    raise ValueError('Monitoring project/tag mismatch')
                if item.metric.type != metric:
                    raise ValueError('Monitoring metric differs from the request')
                for point in item.points:
                    raw_points.append({
                        'metric': metric,
                        'resource': {'type': item.resource.type, 'labels': dict(item.resource.labels)},
                        'labels': dict(item.metric.labels), 'start': str(point.interval.start_time),
                        'end': str(point.interval.end_time), 'value': _point_value(point),
                    })
            # Retain completed metric queries if a subsequent query fails.
            self.raw_points = deduplicate_points(raw_points)
        return UsageObservation(
            sum_usage(p['value'] for p in self.raw_points if p['metric'] == COMPLETED_METRIC),
            sum_usage(p['value'] for p in self.raw_points if p['metric'] == IN_PROGRESS_METRIC), self.raw_points)
