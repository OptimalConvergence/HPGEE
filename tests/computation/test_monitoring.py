from datetime import datetime, timezone

from hpgee.accounting.usage import summarize_usage, submission_allowed, normalize_operation


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


def test_read_usage_binds_metric_resource_project():
    from google.cloud import monitoring_v3
    from hpgee.accounting.usage import read_usage, COMPLETED, IN_PROGRESS
    class Client:
        def list_time_series(self,*,request,**kwargs):
            assert 'resource.labels.project_id = "demo-project"' in request['filter']
            metric=IN_PROGRESS if IN_PROGRESS in request['filter'] else COMPLETED
            return [monitoring_v3.TimeSeries(
                resource={'type':'earthengine_project','labels':{'project_id':'demo-project'}},
                metric={'type':metric,'labels':{'workload_tag':'run-a'}},
                points=[{'interval':{'start_time':datetime(2026,10,13,tzinfo=timezone.utc),
                                    'end_time':datetime(2026,10,13,1,tzinfo=timezone.utc)},
                         'value':{'double_value':3600}}])]
    result=read_usage('demo-project',start='2026-10-13T00:00:00Z',end='2026-10-13T01:00:00Z',workload_tag='run-a',client=Client())
    assert result['status']=='REPORTED' and result['guard_hours']==1


def test_read_usage_rejects_foreign_series_despite_query():
    from google.cloud import monitoring_v3
    from hpgee.accounting.usage import read_usage, COMPLETED
    class Client:
        def list_time_series(self,**kwargs):
            return [monitoring_v3.TimeSeries(
                resource={'type':'earthengine_project','labels':{'project_id':'foreign-project'}},
                metric={'type':COMPLETED,'labels':{'workload_tag':'run-a'}})]
    result=read_usage('demo-project',start='2026-10-13T00:00:00Z',end='2026-10-13T01:00:00Z',workload_tag='run-a',client=Client())
    assert result['status']=='UNKNOWN' and result['error_type']=='ValueError'
