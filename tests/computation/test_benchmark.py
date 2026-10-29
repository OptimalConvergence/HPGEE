import json
import pytest
from geecomputation.benchmark import benchmark_specs,estimate_year,read_metrics
from geecomputation.config import Config
from .test_contract import configuration


def cfg_with_labels(tmp_path):
    raw=configuration(); split=tmp_path/'split.json'
    split.write_text(json.dumps({'events':{'one':'train'},'supports':{'one':'train'}}))
    raw['labels']={'asset':'projects/example/assets/frozen','frozen_split_manifest':str(split)}
    return Config.from_dict(raw)


def test_complexity_specs_freeze_inputs_and_separate_aux(tmp_path):
    specs=benchmark_specs(cfg_with_labels(tmp_path))
    assert len(specs)==6
    assert {(s['trees'],s['feature_set']) for s in specs}=={(t,f) for t in (50,100,200) for f in ('core6','core6_dem')}
    assert len({s['comparison_contract_sha256'] for s in specs})==1
    assert len({s['run_spec_sha256'] for s in specs})==6
    assert specs[0]['sample_snapshot']=='freeze first read; verify identical for every variant'
    for s in specs:
        assert 'GEDI' not in s['detection_inputs'] and 'VIIRS_FRP' not in s['detection_inputs']
        assert 'GEDI' in s['process_aux']


def coverage():
    return [{'stratum':'event','activity':'event','area_month_km2':100},
            {'stratum':'quiet','activity':'low_activity_non_event','area_month_km2':900}]


def synthetic_metrics():
    return [{'stratum':'event','area_month_km2':10,'compute_wall_seconds':20,'eecu_seconds':30,
             'payload_bytes':1000,'queue_seconds':2,'synthetic':True,'telemetry_complete':True,'run_id':'fake1'},
            {'stratum':'quiet','area_month_km2':10,'compute_wall_seconds':10,'eecu_seconds':20,
             'payload_bytes':500,'queue_seconds':3,'synthetic':True,'telemetry_complete':True,'run_id':'fake2'}]


def test_no_metrics_and_synthetic_cannot_become_real_estimate():
    assert estimate_year([],coverage())['status']=='cannot_estimate'
    assert estimate_year(synthetic_metrics(),coverage())['status']=='cannot_estimate'
    result=estimate_year(synthetic_metrics(),coverage(),allow_synthetic=True,bandwidth_bytes_per_second=100)
    assert result['status']=='synthetic_only'
    assert result['eecu_seconds']['point']==2100
    assert result['serial_compute_wall_seconds']['point']==1100
    assert result['payload_bytes']['point']==55000
    assert result['transfer_seconds_at_declared_bandwidth']['point']==550
    assert result['production_wall_seconds'] is None
    assert result['queue_seconds_observed_total']==5


def test_unknown_eecu_or_missing_quiet_support_cannot_estimate():
    rows=synthetic_metrics(); rows[0]['eecu_seconds']=None
    assert estimate_year(rows,coverage(),allow_synthetic=True)['status']=='cannot_estimate'
    assert estimate_year(synthetic_metrics()[:1],coverage(),allow_synthetic=True)['status']=='cannot_estimate'
    assert estimate_year(synthetic_metrics(),coverage()[:1],allow_synthetic=True)['status']=='cannot_estimate'


def test_metric_validation_and_observed_range():
    rows=synthetic_metrics(); second=dict(rows[0],run_id='fake3',compute_wall_seconds=40,eecu_seconds=60,payload_bytes=2000)
    result=estimate_year(rows+[second],coverage(),allow_synthetic=True)
    assert result['eecu_seconds']=={'point':2250,'low':2100,'high':2400}
    assert result['uncertainty']['confidence_interval'] is False
    rows[0]['compute_wall_seconds']=-1
    with pytest.raises(ValueError): estimate_year(rows,coverage(),allow_synthetic=True)


def test_csv_ingestion_preserves_unknown_not_zero(tmp_path):
    path=tmp_path/'metrics.csv'
    path.write_text('stratum,area_month_km2,compute_wall_seconds,eecu_seconds,payload_bytes,queue_seconds,synthetic,run_id\nquiet,10,20,,1000,,true,fake\n')
    row=read_metrics(path)[0]
    assert row['eecu_seconds'] is None and row['queue_seconds'] is None and row['synthetic'] is True


def test_estimator_cli_has_no_real_values_without_metrics(tmp_path,capsys):
    from geecomputation.benchmark import main
    metrics=tmp_path/'empty.json'; metrics.write_text('[]')
    target=tmp_path/'coverage.json'; target.write_text(json.dumps({'year':2020,'months':12,'synthetic':False,'strata':coverage()}))
    assert main(['estimate','--metrics',str(metrics),'--coverage',str(target),'--year','2020'])==2
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='cannot_estimate' and 'eecu_seconds' not in result


def test_explicit_probe_journals_unknown_eecu_without_batch_submission(tmp_path,monkeypatch):
    import ee
    from ee import apitestcase
    from dataclasses import replace
    from geecomputation import cli,monitoring
    from geecomputation.benchmark import execute_benchmark
    from geecomputation.features import DEM_BANDS
    from .test_contract import row
    helper=apitestcase.ApiTestCase(); helper.setUp()
    try:
        cfg=replace(cfg_with_labels(tmp_path),output=tmp_path/'results')
        records={'features':[{'type':'Feature','geometry':None,'properties':dict(row(label),**dict.fromkeys(DEM_BANDS,.1))} for label in (0,1)]}
        def rpc(graph):
            if isinstance(graph,ee.List): return records['features']
            tree=json.loads(graph.serialize()); root=tree['values'][tree['result']]
            name=root['functionInvocationValue']['functionName']
            if name=='Image.reduceRegion':
                assert root['functionInvocationValue']['arguments']['maxPixels']['constantValue']>=32768
                return {'score_burned_mean':.5,'score_unburned_mean':.5,'score_burned_count':10,'score_unburned_count':10}
            if name=='Dictionary.select': return {'importance':{},'numberOfTrees':100}
            pytest.fail('unexpected external RPC: '+name)
        monkeypatch.setattr(ee.data,'computeValue',rpc)
        monkeypatch.setattr(cli,'initialize',lambda cfg:ee)
        monkeypatch.setattr(monitoring,'read_usage',lambda project,**kw:
            monitoring.summarize_usage([100],[],start=kw['start'],end=kw['end']))
        result=execute_benchmark(cfg)
        assert len(result['records'])==6
        assert all(r['status']=='evaluated' and r['eecu_seconds'] is None and r['reported_eecu_seconds']==100 and r['telemetry_complete'] is False for r in result['records'])
        assert len({r['remote_workload_tag'] for r in result['records']})==6
        assert len({r['sample_snapshot_sha256'] for r in result['records']})==1
        assert all(r['remote_operation'] is None and r['payload_bytes'] is None for r in result['records'])
        assert (tmp_path/'results'/'benchmarks'/result['run_id']/'records.json').exists()
    finally:
        helper.tearDown(); ee.Reset()


def test_partial_reported_points_cannot_estimate_even_when_nonzero():
    rows=synthetic_metrics()
    for row in rows: row['telemetry_complete']=False
    assert estimate_year(rows,coverage(),allow_synthetic=True)['status']=='cannot_estimate'


def test_refresh_only_telemetry_preserves_tag_window_and_never_completes(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    from geecomputation import monitoring
    from geecomputation.benchmark import refresh_telemetry
    from dataclasses import replace
    cfg=replace(cfg_with_labels(tmp_path),output=tmp_path/'out')
    path=tmp_path/'records.json'
    path.write_text(json.dumps([{'run_id':'existing','project':cfg.project,'remote_workload_tag':'existing-tag',
        'start':'2026-10-13T01:00:00Z','end':'2026-10-13T01:01:00Z','telemetry_complete':False,'eecu_seconds':None}]))
    def measured(project,**kw):
        assert kw['workload_tag']=='existing-tag'
        assert kw['start']=='2026-10-13T01:00:00Z'
        assert kw['end']=='2026-10-13T01:06:00Z'
        return monitoring.summarize_usage([100],[],start=kw['start'],end=kw['end'])
    monkeypatch.setattr(monitoring,'read_usage',measured)
    result=refresh_telemetry(cfg,path,delay_seconds=300,now=datetime(2026,10,13,2,tzinfo=timezone.utc))
    row=result['records'][0]
    assert row['reported_eecu_seconds']==100
    assert row['eecu_seconds'] is None and row['telemetry_complete'] is False
    assert row['telemetry_window']['original_end']=='2026-10-13T01:01:00Z'
    assert row['remote_workload_tag']=='existing-tag'


def test_refresh_delay_never_queries_early(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    from geecomputation import monitoring
    from geecomputation.benchmark import refresh_telemetry
    cfg=cfg_with_labels(tmp_path)
    path=tmp_path/'records.json'; path.write_text(json.dumps([{'project':cfg.project,'remote_workload_tag':'tag',
        'start':'2026-10-13T01:00:00Z','end':'2026-10-13T01:01:00Z'}]))
    monkeypatch.setattr(monitoring,'read_usage',lambda *args,**kw:pytest.fail('delay window has not elapsed'))
    result=refresh_telemetry(cfg,path,delay_seconds=300,now=datetime(2026,10,13,1,2,tzinfo=timezone.utc))
    assert result['records'][0]['telemetry_refresh_status']=='delay_window_not_elapsed'
    assert result['records'][0]['telemetry_complete'] is False


def test_real_metrics_need_independent_completion_evidence():
    rows=synthetic_metrics()
    for row in rows: row.update(synthetic=False,variant='t100-core6',pipeline_id='frozen-pipeline')
    result=estimate_year(rows,coverage())
    assert result['status']=='cannot_estimate' and 'reconciliation evidence' in result['reason']


def test_csv_complete_flag_and_reconciliation_evidence_are_explicit(tmp_path):
    path=tmp_path/'measured.csv'
    path.write_text('stratum,area_month_km2,compute_wall_seconds,eecu_seconds,payload_bytes,queue_seconds,synthetic,telemetry_complete,run_id,telemetry_confirmation_method,telemetry_confirmation_evidence_reference\nquiet,10,20,30,1000,,false,true,run-one,manual_reconciliation,review-record.json\n')
    row=read_metrics(path)[0]
    assert row['telemetry_complete'] is True
    assert row['telemetry_confirmation']=={'method':'manual_reconciliation','evidence_reference':'review-record.json'}
