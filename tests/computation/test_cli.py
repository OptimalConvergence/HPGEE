import json
import pytest
from hpgee.cli.computation import main
from .test_contract import configuration


def test_manifest_cli_runs_without_ee_authentication(tmp_path,capsys):
    raw=configuration(); raw['output']=str(tmp_path/'out')
    config=tmp_path/'config.json'; config.write_text(json.dumps(raw))
    assert main(['manifest','--config',str(config)]) == 0
    emitted=json.loads(capsys.readouterr().out)
    assert emitted['reflectance_scale']==1
    assert emitted['grid']['dimensions']['width']==128
    assert (tmp_path/'out'/'recipe-manifest.json').is_file()


def test_cli_rejects_training_without_frozen_asset_contract(tmp_path):
    raw=configuration(); path=tmp_path/'config.json'; path.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='labels'):
        main(['train','--config',str(path)])


def test_usage_unknown_returns_nonzero_without_ee_initialize(tmp_path,capsys,monkeypatch):
    import hpgee.cli.computation as cli; import hpgee.accounting.usage as monitoring
    raw=configuration(); raw['output']=str(tmp_path/'out')
    path=tmp_path/'config.json'; path.write_text(json.dumps(raw))
    def unavailable(project,*,start,end,workload_tag=None,credentials=None):
        return monitoring.summarize_usage([],[],start=start,end=end,error='PermissionDenied')
    monkeypatch.setattr(monitoring,'read_usage',unavailable)
    monkeypatch.setattr(cli,'initialize',lambda cfg: pytest.fail('usage must not initialize EE'))
    assert main(['usage','--config',str(path),'--start','2026-10-13T00:00:00Z',
                 '--end','2026-10-13T01:00:00Z']) == 2
    assert json.loads(capsys.readouterr().out)['status']=='UNKNOWN'


def test_status_rejects_non_operation_before_network(tmp_path):
    raw=configuration(); path=tmp_path/'config.json'; path.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='operation'):
        main(['status','--config',str(path),'--operation','some-other-resource'])


def test_existing_output_does_not_silently_change_scientific_config(tmp_path,capsys):
    raw=configuration(); raw['output']=str(tmp_path/'out')
    path=tmp_path/'config.json'; path.write_text(json.dumps(raw))
    main(['manifest','--config',str(path)])
    raw['pre_start']='2020-06-01'; path.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='configuration'):
        main(['manifest','--config',str(path)])


def test_quota_cli_is_readonly_and_does_not_initialize_ee(tmp_path,capsys,monkeypatch):
    import hpgee.cli.computation as cli; import hpgee.accounting.quota as quota
    raw=configuration(); raw['output']=str(tmp_path/'out')
    path=tmp_path/'config.json'; path.write_text(json.dumps(raw))
    monkeypatch.setattr(cli,'initialize',lambda cfg:pytest.fail('quota must not initialize EE'))
    monkeypatch.setattr(quota,'read_quota',lambda project,project_number=None,credentials=None:
                        {'quota':{'status':'UNKNOWN'},'monthly_remaining_eecu_hours':None,'project_number':project_number})
    assert main(['quota','--config',str(path),'--project-number','123'])==2
    assert json.loads(capsys.readouterr().out)['monthly_remaining_eecu_hours'] is None
