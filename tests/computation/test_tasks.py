import json
import pytest
from geecomputation.tasks import submit_once


def test_foreign_usage_cannot_submit(tmp_path):
    with pytest.raises(ValueError, match='Usage project'):
        submit_once({}, journal=tmp_path/'tasks.jsonl', project='demo-project', workload_tag='test',
                    usage={'project':'other-project','status':'REPORTED','guard_hours':0},
                    reserve_hours=1, limit_hours=10, export=lambda *args: pytest.fail('must not submit'))
    assert not (tmp_path/'tasks.jsonl').exists()


def test_actual_ee_project_must_match_before_task_access(tmp_path, monkeypatch):
    import ee
    from geecomputation.tasks import submit_ee_export
    monkeypatch.setattr(ee.data, '_get_projects_path', lambda:'projects/other-project')
    with pytest.raises(ValueError, match='Initialized Earth Engine project'):
        submit_ee_export(None, contract={}, journal=tmp_path/'tasks.jsonl',project='demo-project',
                         workload_tag='test',usage={},reserve_hours=1,limit_hours=10)
    assert not (tmp_path/'tasks.jsonl').exists()


def test_success_is_reused_and_spec_change_gets_different_identity(tmp_path):
    calls = []
    def export(request_id, description):
        calls.append(request_id)
        return {'name': 'projects/demo/operations/' + request_id}
    kwargs = dict(journal=tmp_path/'tasks.jsonl', project='demo-project', workload_tag='test',
                  usage={'project':'demo-project','workload_tag':'test','status':'REPORTED','guard_hours':1}, reserve_hours=1, limit_hours=10,
                  export=export)
    first = submit_once({'recipe':'a'}, **kwargs)
    second = submit_once({'recipe':'a'}, **kwargs)
    third = submit_once({'recipe':'b'}, **kwargs)
    assert first['name'] == second['name']
    assert len(calls) == 2
    assert third['name'] != first['name']


def test_ambiguous_submission_never_auto_resubmits(tmp_path):
    calls = []
    def export(request_id, description):
        calls.append(request_id)
        raise TimeoutError()
    kwargs = dict(journal=tmp_path/'tasks.jsonl', project='demo-project', workload_tag='test',
                  usage={'project':'demo-project','workload_tag':'test','status':'REPORTED','guard_hours':1}, reserve_hours=1, limit_hours=10,
                  export=export)
    with pytest.raises(TimeoutError):
        submit_once({'recipe':'a'}, **kwargs)
    result = submit_once({'recipe':'a'}, **kwargs)
    assert result['state'] == 'SUBMISSION_UNKNOWN'
    assert len(calls) == 1
    assert result['request_id']


def test_unknown_budget_cannot_start_task(tmp_path):
    with pytest.raises(RuntimeError, match='budget'):
        submit_once({'recipe':'a'}, journal=tmp_path/'tasks.jsonl', project='demo-project',
                    workload_tag='test', usage={'project':'demo-project','workload_tag':'test','status':'UNKNOWN','guard_hours':None},
                    reserve_hours=1, limit_hours=10,
                    export=lambda *args: pytest.fail('must not submit'))
