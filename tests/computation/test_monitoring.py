from datetime import datetime, timezone

from geecomputation.monitoring import summarize_usage, submission_allowed, normalize_operation


def test_empty_telemetry_is_unknown_not_free_compute():
    usage = summarize_usage([], [], start='2026-10-13T00:00:00Z', end='2026-10-13T01:00:00Z')
    assert usage['status'] == 'UNKNOWN'
    assert usage['guard_hours'] is None
    assert not submission_allowed(usage, 10000, reserve_hours=1)


def test_completed_and_inprogress_are_not_added():
    usage = summarize_usage([3600, 1800], [7200], start='2026-10-13T00:00:00Z', end='2026-10-13T01:00:00Z')
    assert usage['guard_hours'] == 2
    assert not submission_allowed(usage, 2.5, reserve_hours=0.6)
    assert submission_allowed(usage, 3, reserve_hours=0.5)


def test_failed_operation_is_not_product_acceptance():
    result = normalize_operation({'name': 'projects/p/operations/x', 'done': True,
                                  'error': {'code': 13, 'message': 'remote failure'},
                                  'metadata': {'state': 'FAILED', 'attempt': 2, 'batchEecuUsageSeconds': 10}})
    assert result['state'] == 'FAILED'
    assert result['attempt_eecu_seconds'] == 10
    assert result['artifact_verified'] is False


def test_usage_rejects_invalid_intervals_and_costs():
    import pytest
    with pytest.raises(ValueError):
        summarize_usage([1], [], start='2026-10-14T00:00:00Z', end='2026-10-13T00:00:00Z')
    with pytest.raises(ValueError):
        summarize_usage([-1], [], start='2026-10-13T00:00:00Z', end='2026-10-14T00:00:00Z')


def test_read_usage_binds_metric_resource_project(monkeypatch):
    from types import SimpleNamespace as N
    from google.cloud import monitoring_v3
    from geecomputation.monitoring import read_usage
    point=N(value=N(_pb=N(WhichOneof=lambda _: 'double_value'),double_value=3600.0))
    series=N(resource=N(labels={'project_id':'demo-project'}),metric=N(labels={'workload_tag':'run-a'}),points=[point])
    class Client:
        def __init__(self,**kwargs): pass
        def list_time_series(self,*,request,**kwargs):
            assert 'resource.labels.project_id = "demo-project"' in request['filter']
            return [series]
    monkeypatch.setattr(monitoring_v3,'MetricServiceClient',Client)
    result=read_usage('demo-project',start='2026-10-13T00:00:00Z',end='2026-10-13T01:00:00Z',workload_tag='run-a',credentials=object())
    assert result['status']=='REPORTED' and result['guard_hours']==1


def test_read_usage_rejects_foreign_series_despite_query(monkeypatch):
    from types import SimpleNamespace as N
    from google.cloud import monitoring_v3
    from geecomputation.monitoring import read_usage
    class Client:
        def __init__(self,**kwargs): pass
        def list_time_series(self,**kwargs):
            return [N(resource=N(labels={'project_id':'foreign-project'}),metric=N(labels={'workload_tag':'run-a'}),points=[])]
    monkeypatch.setattr(monitoring_v3,'MetricServiceClient',Client)
    result=read_usage('demo-project',start='2026-10-13T00:00:00Z',end='2026-10-13T01:00:00Z',workload_tag='run-a',credentials=object())
    assert result['status']=='UNKNOWN' and result['error_type']=='ValueError'
