import json
import re
import pytest
import hpgee.accounting.usage as m
from hpgee.accounting.tasks import submit_once


def test_stable_logical_task_survives_changed_tag_budget_and_plan(tmp_path):
    calls=[]
    def export(*args):
        calls.append(args)
        raise TimeoutError()
    kw=dict(journal=tmp_path/'journal',project='demo-project',workload_tag='first',
            usage={'project':'demo-project','status':'REPORTED','guard_hours':0},
            reserve_hours=1,limit_hours=10,export=export,logical_task_id='logical-a')
    with pytest.raises(TimeoutError): submit_once({'graph':'a'},**kw)
    kw.update(workload_tag='second',limit_hours=20)
    result=submit_once({'graph':'a'},**kw)
    assert result['state']=='SUBMISSION_UNKNOWN' and len(calls)==1
    with pytest.raises(ValueError,match='contract'):
        submit_once({'graph':'changed'},**kw)


def test_legacy_journal_recovers_without_rewriting_or_new_tag_submission(tmp_path):
    import hashlib
    path=tmp_path/'journal'
    identity={'project':'demo-project','workload_tag':'original','spec':{'graph':'a'}}
    fp=hashlib.sha256(json.dumps(identity,sort_keys=True,allow_nan=False).encode()).hexdigest()
    text=json.dumps(dict(spec_hash=fp,project='demo-project',workload_tag='original',state='INTENT',request_id='old',reserve_hours=1))+'\n'
    path.write_text(text)
    row=submit_once({'graph':'a'},journal=path,project='demo-project',workload_tag='new',
                    usage={'project':'demo-project','status':'UNKNOWN'},reserve_hours=1,limit_hours=10,
                    export=lambda *args:pytest.fail('recovery must not submit'))
    assert row['request_id']=='old' and row['state']=='SUBMISSION_UNKNOWN'
    assert path.read_text()==text


def test_workload_tags_hash_all_identity_parts():
    tags={m.make_workload_tag(r,u,s,a) for r in ('run-a','run-b') for u in ('u','v')
          for s in ('x'*100+'a','x'*100+'b') for a in (1,2)}
    assert len(tags)==16
    assert all(len(t)<=63 and re.fullmatch('[a-z0-9][a-z0-9_-]*[a-z0-9]',t) for t in tags)


def test_month_boundary_uses_provider_timezone_across_dst():
    assert m.project_month_interval('2026-03-15T00:00:00Z')==('2026-03-01T08:00:00Z','2026-04-01T07:00:00Z')


def test_shared_ledger_settles_without_double_count_and_preserves_attempt_lower_bound(tmp_path):
    kw=dict(project='demo-project',run_id='r',run_limit_hours=10,project_remaining_hours=20,bootstrap_max_hours=.1)
    ledger=m.RunLedger(tmp_path/'ledger.json',**kw)
    ledger.reserve('pilot.json',.01,bootstrap=True)
    ledger.observe('pilot.json',hours=0,attempt=1,complete=True,reference='pilot')
    estimate=dict(pilot_reference='pilot.json',estimate_hours=1,safety_factor=2,unknowns=['delay'])
    ledger.reserve('batch',2,estimate=estimate)
    ledger=m.RunLedger(tmp_path/'ledger.json',**kw)
    ledger.observe('batch',hours=1,attempt=1,complete=True,reference='operation/a')
    assert ledger.summary()['charged_hours']==1
    ledger.observe('batch',hours=.5,attempt=2,complete=False,reference='operation/a')
    summary=ledger.summary()
    assert summary['reported_lower_bound_hours']==1.5 and summary['unsettled_reservations_hours']==2
    with pytest.raises(RuntimeError,match='unsettled'):
        ledger.reserve('sampler',2,estimate=estimate)


def test_bootstrap_is_explicit_single_small_unit_and_shared_budget(tmp_path):
    ledger=m.RunLedger(tmp_path/'ledger',project='demo-project',run_id='r',run_limit_hours=3,
                       project_remaining_hours=2,bootstrap_max_hours=.1)
    with pytest.raises(ValueError,match='estimate'):
        ledger.reserve('unknown',1)
    ledger.reserve('pilot',.1,bootstrap=True)
    with pytest.raises(RuntimeError): ledger.reserve('pilot2',.1,bootstrap=True)
    ledger.observe('pilot',hours=.05,attempt=1,complete=True,reference='tag/pilot')
    with pytest.raises(RuntimeError,match='budget'):
        ledger.reserve('sampler',2,estimate=dict(pilot_reference='pilot',estimate_hours=1,safety_factor=2,unknowns=[]))


def test_monitoring_point_identity_deduplicates_snapshots():
    point=dict(metric='completed',resource={'type':'project','labels':{'project':'p'}},
               labels={'workload_tag':'a'},start='2026-01-01T00:00:00Z',end='2026-01-01T01:00:00Z',value=3)
    assert m.deduplicate_points([point,dict(point,value=4)])==[dict(point,value=4)]


def test_config_budget_is_validation_not_historical_authorization():
    from hpgee.workflows.config import Config
    from .test_contract import configuration
    raw=configuration();raw['budget_eecu_hours']=10001
    assert Config.from_dict(raw).budget_eecu_hours==10001


def test_export_requires_explicit_execution_budget(tmp_path):
    from hpgee.accounting.tasks import submit_ee_export
    with pytest.raises((TypeError,ValueError),match='limit_hours|budget'):
        submit_ee_export(None,contract={},journal=tmp_path/'j',project='demo-project',
                         workload_tag='x',usage={},reserve_hours=1)


def test_stage_nested_exception_restores_actual_tag(tmp_path,monkeypatch):
    import ee
    from contextlib import nullcontext
    from hpgee.workflows.detection import StageRecorder
    from hpgee.workflows.config import Config
    from .test_contract import configuration
    current=['original']
    monkeypatch.setattr(ee.data,'getWorkloadTag',lambda:current[0])
    monkeypatch.setattr(ee.data,'setWorkloadTag',lambda x:current.__setitem__(0,x))
    monkeypatch.setattr(ee,'profilePrinting',lambda **kw:nullcontext())
    recorder=StageRecorder(Config.from_dict(configuration()),tmp_path,run_id='fixed-run',profile=False)
    with recorder.stage('outer',unit_id='u') as outer:
        with pytest.raises(RuntimeError):
            with recorder.stage('inner',unit_id='u'):
                raise RuntimeError('failed')
        assert current[0]==outer['workload_tag']
    assert current[0]=='original'
    with recorder.stage('outer',unit_id='u') as again: pass
    assert again['workload_tag']!=outer['workload_tag']
    resumed=StageRecorder(Config.from_dict(configuration()),tmp_path,run_id='fixed-run',profile=False)
    with resumed.stage('outer',unit_id='u') as last: pass
    assert last['client_attempt']==3


def test_read_usage_retains_deduplicated_raw_points():
    from types import SimpleNamespace as N
    point=N(interval=N(start_time='2026-01-01T00:00:00Z',end_time='2026-01-01T01:00:00Z'),
            value=N(_pb=N(WhichOneof=lambda _: 'double_value'),double_value=3600.0))
    class Client:
        def list_time_series(self,*,request,**kwargs):
            metric=m.IN_PROGRESS if m.IN_PROGRESS in request['filter'] else m.COMPLETED
            return [N(resource=N(type='project',labels={'project_id':'demo-project'}),
                      metric=N(type=metric,labels={'workload_tag':'run-a'}),points=[point,point])]
    usage=m.read_usage('demo-project',start='2026-01-01T00:00:00Z',end='2026-01-02T00:00:00Z',workload_tag='run-a',client=Client())
    assert usage['guard_hours']==1 and len(usage['raw_points'])==2
    assert usage['telemetry_complete'] is False


def test_logical_identity_excludes_plan_but_includes_trial_and_export():
    from hpgee.accounting.tasks import make_logical_task_id
    kw=dict(unit_id='u',trial_id='production-r',backend='asset',export_contract={'graph':'a','destination':'a'})
    identity=make_logical_task_id(**kw)
    assert identity!=make_logical_task_id(**dict(kw,trial_id='trial-2'))
    assert identity!=make_logical_task_id(**dict(kw,export_contract={'graph':'a','destination':'b'}))


def test_distinct_trials_recover_original_operations_and_active_limit(tmp_path):
    kw=dict(journal=tmp_path/'j',project='demo-project',workload_tag='tag',
            usage={'project':'demo-project','status':'REPORTED','guard_hours':0},reserve_hours=1,limit_hours=10,
            export=lambda request_id,desc:{'name':'operations/'+request_id})
    a=submit_once({'graph':'a'},logical_task_id='trial-a',**kw)
    b=submit_once({'graph':'a'},logical_task_id='trial-b',**kw)
    assert a['name']!=b['name']
    from hpgee.accounting.tasks import refresh_known
    rows=refresh_known(journal=tmp_path/'j',get_operation=lambda name:{'name':name,'metadata':{'state':'RUNNING'}})
    assert len(rows)==2
    with pytest.raises(RuntimeError,match='active-task'):
        submit_once({'graph':'b'},logical_task_id='trial-c',**kw)


def test_refresh_operation_retains_server_attempt_cost_history(tmp_path):
    from hpgee.accounting.tasks import refresh_known
    submit_once({},journal=tmp_path/'j',project='demo-project',workload_tag='a',
                usage={'project':'demo-project','status':'REPORTED','guard_hours':0},reserve_hours=1,limit_hours=10,
                export=lambda *args:{'name':'operations/a'})
    for attempt,cost in [(1,10),(1,12),(2,5)]:
        rows=refresh_known(journal=tmp_path/'j',get_operation=lambda name:{'name':name,'metadata':{'state':'RUNNING','attempt':attempt,'batchEecuUsageSeconds':cost}})
    row=rows[0]
    assert row['operation_lower_bound_seconds']==17
    assert len(row['operation_observations'])==3


def test_ledger_cannot_charge_same_operation_to_two_units(tmp_path):
    ledger=m.RunLedger(tmp_path/'j',project='demo-project',run_id='r',run_limit_hours=10,project_remaining_hours=10,bootstrap_max_hours=.1)
    ledger.reserve('p',.01,bootstrap=True)
    ledger.observe('p',hours=0,attempt=1,complete=True,reference='pilot')
    estimate=dict(pilot_reference='p',estimate_hours=1,safety_factor=1,unknowns=[])
    ledger.reserve('a',1,estimate=estimate);ledger.reserve('b',1,estimate=estimate)
    ledger.observe('a',hours=.1,attempt=1,complete=True,reference='operation/shared')
    with pytest.raises(ValueError,match='reference'):
        ledger.observe('b',hours=.1,attempt=1,complete=True,reference='operation/shared')


def test_unknown_budget_needs_bounded_bootstrap_or_reconciled_pilot(tmp_path):
    ledger=m.RunLedger(tmp_path/'j',project='demo-project',run_id='r',run_limit_hours=10,project_remaining_hours=10)
    with pytest.raises(ValueError,match='pilot'):
        ledger.reserve('a',1,estimate={'pilot_reference':'invented','estimate_hours':1,'safety_factor':1,'unknowns':[]},require_verified_pilot=True)


def test_legacy_export_spec_included_original_workload_tag(tmp_path,monkeypatch):
    import ee
    import hashlib
    from types import SimpleNamespace as N
    from hpgee.accounting.tasks import submit_ee_export
    monkeypatch.setattr(ee.data,'_get_projects_path',lambda:'projects/demo-project')
    monkeypatch.setattr(ee.serializer,'encode',lambda config,**kw:json.dumps(config,sort_keys=True))
    task=N(task_type=ee.batch.Task.Type.EXPORT_IMAGE,config={'assetId':'destination','description':'prepared-name'})
    spec={'contract':{},'task_type':str(task.task_type),'expression':json.dumps(dict(task.config,workloadTag='old'),sort_keys=True)}
    identity=dict(project='demo-project',workload_tag='old',spec=spec)
    fp=hashlib.sha256(json.dumps(identity,sort_keys=True,allow_nan=False).encode()).hexdigest()
    path=tmp_path/'j';path.write_text(json.dumps(dict(project='demo-project',workload_tag='old',spec_hash=fp,state='INTENT',request_id='old'))+'\n')
    before=path.read_bytes()
    row=submit_ee_export(task,contract={},journal=path,project='demo-project',workload_tag='new',
                         usage={'project':'demo-project','status':'UNKNOWN'},reserve_hours=1,limit_hours=10)
    assert row['state']=='SUBMISSION_UNKNOWN' and path.read_bytes()==before


def test_recovery_precedes_new_admission_tag_checks(tmp_path):
    kw=dict(journal=tmp_path/'j',project='demo-project',workload_tag='original',
            usage={'project':'demo-project','workload_tag':'original','status':'REPORTED','guard_hours':0},
            reserve_hours=1,limit_hours=10,export=lambda *args:{'name':'operations/a'})
    first=submit_once({},**kw)
    kw['workload_tag']='new-attempt'
    assert submit_once({},**kw)['request_id']==first['request_id']
