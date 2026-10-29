"""Offline contracts shared by cumulative and fixed-interval EE accounting."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import ee
import pytest
from google.auth.transport import requests as auth_requests
from google.cloud import monitoring_v3

from hpgee.accounting import sampling, usage

START = datetime(2026, 10, 25, 18, tzinfo=timezone.utc)
END = datetime(2026, 10, 25, 19, tzinfo=timezone.utc)
PROJECT = 'project-x'
TAG = 'run-a'


def series(metric, value, *, tag=TAG):
    return monitoring_v3.TimeSeries({
        'metric': {'type': metric, 'labels': {'workload_tag': tag}},
        'resource': {'type': 'earthengine_project', 'labels': {'project_id': PROJECT}},
        'points': [{'interval': {'start_time': START, 'end_time': END},
                    'value': {'double_value': value}}],
    })


class PagedSession:
    def __init__(self):
        self.calls = []

    def get(self, url, *, params, timeout):
        self.calls.append((url, dict(params), timeout))
        metric = (sampling.IN_PROGRESS_METRIC if sampling.IN_PROGRESS_METRIC in params['filter']
                  else sampling.COMPLETED_METRIC)
        values = [7200] if metric == sampling.IN_PROGRESS_METRIC else [3600, 3600]
        data = {'timeSeries': [json.loads(monitoring_v3.TimeSeries.to_json(series(metric, value)))
                               for value in values]}
        if metric == sampling.COMPLETED_METRIC and 'pageToken' not in params:
            data['nextPageToken'] = 'next'
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)


def install_rest(monkeypatch):
    sessions = []

    def authorized_session(credentials):
        session = PagedSession()
        sessions.append((credentials, session))
        return session

    def no_grpc(**kwargs):
        raise RuntimeError('Accounting must use the common proxy-aware REST path')

    monkeypatch.setattr(auth_requests, 'AuthorizedSession', authorized_session)
    monkeypatch.setattr(monitoring_v3, 'MetricServiceClient', no_grpc)
    monkeypatch.setattr(sampling, 'datetime', SimpleNamespace(now=lambda tz: END))
    return sessions


def test_fixed_and_cumulative_views_use_same_rest_pages_and_overlap_guard(monkeypatch):
    sessions = install_rest(monkeypatch)
    credentials = object()
    monkeypatch.setattr(ee.data, '_get_state',
                        lambda: pytest.fail('Explicit credentials must take precedence'))
    reader = sampling.CloudEECUReader(PROJECT, TAG, start=START, credentials=credentials)
    snapshot = reader.read()
    report = usage.read_usage(PROJECT, start=START.isoformat(), end=END.isoformat(),
                              workload_tag=TAG, credentials=credentials)

    assert report['status'] == 'REPORTED'
    assert snapshot.completed_seconds == report['completed_seconds'] == 3600
    assert snapshot.in_progress_seconds == report['in_progress_seconds'] == 7200
    assert snapshot.guard_seconds == report['guard_hours'] * 3600 == 7200
    assert report['telemetry_complete'] is False
    assert len(reader.raw_points) == len(report['raw_points']) == 2
    assert len(sessions) == 2 and all(value is credentials for value, _ in sessions)
    assert all(len(session.calls) == 3 for _, session in sessions)
    assert sessions[0][1].calls == sessions[1][1].calls
    assert reader.raw_points[0]['resource_type'] == 'earthengine_project'
    assert report['raw_points'][0]['resource']['type'] == 'earthengine_project'
    assert 'queried_at' in report['raw_points'][0]


def test_both_views_reuse_initialized_earth_engine_credentials(monkeypatch):
    sessions = install_rest(monkeypatch)
    credentials = object()
    monkeypatch.setattr(ee.data, '_get_state', lambda: SimpleNamespace(credentials=credentials))
    monkeypatch.setattr(ee.data, 'get_persistent_credentials',
                        lambda: pytest.fail('Initialized EE credentials must be reused'))
    sampling.CloudEECUReader(PROJECT, TAG, start=START).read()
    report = usage.read_usage(PROJECT, start=START.isoformat(), end=END.isoformat(), workload_tag=TAG)
    assert report['status'] == 'REPORTED'
    assert len(sessions) == 2 and all(value is credentials for value, _ in sessions)


def test_point_identity_reconciles_both_persisted_schemas_and_utc_spellings():
    sample_point = {
        'metric': sampling.COMPLETED_METRIC, 'resource_type': 'earthengine_project',
        'resource': {'project_id': PROJECT}, 'labels': {'workload_tag': TAG},
        'start': '2026-10-25 18:00:00+00:00', 'end': '2026-10-25 19:00:00+00:00', 'value': 1,
    }
    fixed_point = {
        'metric': sampling.COMPLETED_METRIC,
        'resource': {'type': 'earthengine_project', 'labels': {'project_id': PROJECT}},
        'labels': {'workload_tag': TAG}, 'start': '2026-10-25T20:00:00+02:00',
        'end': '2026-10-25T19:00:00Z', 'value': 2, 'queried_at': 'later',
    }
    assert usage.deduplicate_points([sample_point, fixed_point]) == [fixed_point]


def test_cumulative_reader_rejects_foreign_metric_despite_requested_filter():
    class Client:
        def list_time_series(self, **kwargs):
            return [series(sampling.IN_PROGRESS_METRIC, 42)]

    reader = sampling.CloudEECUReader(PROJECT, TAG, start=START, client=Client())
    with pytest.raises(ValueError, match='metric'):
        reader.read()


@pytest.mark.parametrize('scope', ['project', 'prefix'])
def test_project_and_prefix_scopes_keep_distinct_workload_series(scope, monkeypatch):
    monkeypatch.setattr(sampling, 'datetime', SimpleNamespace(now=lambda tz: END))

    class Client:
        def list_time_series(self, *, request, **kwargs):
            query = request['filter']
            if scope == 'project':
                assert 'metric.labels.workload_tag' not in query
            else:
                assert 'monitoring.regex.full_match("run-.*")' in query
            if sampling.IN_PROGRESS_METRIC in query:
                return []
            return [series(sampling.COMPLETED_METRIC, 3600, tag='run-a'),
                    series(sampling.COMPLETED_METRIC, 1800, tag='run-b')]

    if scope == 'project':
        report = usage.read_usage(PROJECT, start=START.isoformat(), end=END.isoformat(), client=Client())
        assert report['status'] == 'REPORTED' and report['guard_hours'] == 1.5
        points = report['raw_points']
    else:
        reader = sampling.CloudEECUReader(PROJECT, 'run-', start=START, tag_prefix=True, client=Client())
        assert reader.read().guard_seconds == 5400
        points = reader.raw_points
    assert len(points) == 2


@pytest.mark.parametrize('failure', ['empty', 'timeout'])
def test_fixed_rest_absence_and_transport_failure_remain_unknown(monkeypatch, failure):
    class Session:
        def get(self, *args, **kwargs):
            if failure == 'timeout':
                raise TimeoutError('offline timeout')
            return SimpleNamespace(raise_for_status=lambda: None, json=dict)

    monkeypatch.setattr(auth_requests, 'AuthorizedSession', lambda credentials: Session())
    report = usage.read_usage(PROJECT, start=START.isoformat(), end=END.isoformat(),
                              workload_tag=TAG, credentials=object())
    assert report['status'] == 'UNKNOWN'
    assert report['completed_seconds'] is report['in_progress_seconds'] is report['guard_hours'] is None
    assert report['error_type'] == ('TimeoutError' if failure == 'timeout' else None)


def test_failed_cumulative_poll_preserves_partial_raw_points_without_claiming_totals(tmp_path, monkeypatch):
    from hpgee.core.models import EECUMonitorConfig

    monkeypatch.setattr(sampling, 'datetime', SimpleNamespace(now=lambda tz: END))

    class Client:
        def list_time_series(self, *, request, **kwargs):
            if sampling.IN_PROGRESS_METRIC in request['filter']:
                raise TimeoutError('offline in-progress failure')
            return [series(sampling.COMPLETED_METRIC, 3600)]

    reader = sampling.CloudEECUReader(PROJECT, TAG, start=START, client=Client())
    path = tmp_path / 'usage.jsonl'
    monitor = sampling.EECUMonitor(EECUMonitorConfig(), reader, path, lambda: 1)
    assert monitor.poll_once() is None
    report = json.loads(path.read_text())
    assert report['available'] is False and report['error'] == 'TimeoutError'
    assert report['completed_seconds'] is report['in_progress_seconds'] is None
    assert len(report['raw_points']) == 1 and report['raw_points'][0]['value'] == 3600
def test_legacy_monitoring_client_factory_remains_an_injection_point(monkeypatch):
    import geesampler.monitoring as legacy
    from hpgee.accounting.sampling import CloudEECUReader

    client = object()
    credentials = object()
    seen = []

    def factory(*, credentials):
        seen.append(credentials)
        return client

    monkeypatch.setattr(legacy, 'MonitoringRESTClient', factory)
    reader = CloudEECUReader('example-project', 'run', credentials=credentials)
    assert reader._reader.client is client
    assert seen == [credentials]
