from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace as N
import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine
from hpgee.transfer.delivery import verify_delivery, sampler_arguments, deliver_unit
from hpgee.planning.cube import build_cube_plan
from .test_planning import manifest,grid,support


BANDS=['blue','green','red','nir','swir1','swir2','Fmask','source_valid_bits','clear']


def plan():
    g=grid(2);g['chip_size']=2
    return build_cube_plan(source_manifest=manifest(),supports=[support('original',(500000,999940,500060,1000000),'reference_negative')],grid=g,
                           window={'start':'2025-12-01','end':'2026-01-01'},bands=BANDS)


def saved(tmp_path,p,values=None):
    unit=p['units'][0];path=tmp_path/'source.tif'
    if values is None:
        values=np.ones((9,2,2),dtype='float32');values[6]=0;values[7]=127;values[8]=1
    with rasterio.open(path,'w',driver='GTiff',width=2,height=2,count=9,dtype='float32',crs='EPSG:32635',transform=Affine(30,0,500000,0,-30,1000000),nodata=np.nan) as dst:
        dst.write(values);dst.descriptions=tuple(BANDS)
    return dict(unit_id=unit['unit_id'],path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),status='verified',
                scene_id=unit['scene']['system_index'],scene_time_ms=unit['scene']['time_ms'],band_order=BANDS)


def test_saved_validity_and_original_draw_coverage(tmp_path):
    p=plan();receipt=saved(tmp_path,p)
    result=verify_delivery({'plan':p,'deliveries':[receipt]})
    assert result['status']=='complete' and result['supports'][0]['requested_pixels']==4
    assert result['supports'][0]['observed_pixels']==4
    assert result['scientific_label_eligibility_changed'] is False


def test_unknown_fmask_remains_unknown_and_not_clear(tmp_path):
    p=plan();values=np.ones((9,2,2),dtype='float32');values[6]=np.nan;values[7]=63;values[8]=-1
    receipt=saved(tmp_path,p,values)
    assert verify_delivery({'plan':p,'deliveries':[receipt]})['status']=='complete'
    values[8]=1;receipt=saved(tmp_path,p,values)
    with pytest.raises(ValueError,match='clear'):
        verify_delivery({'plan':p,'deliveries':[receipt]})


def test_missing_and_corrupt_saved_files_do_not_pass(tmp_path):
    p=plan()
    assert verify_delivery({'plan':p,'deliveries':[]})['status']=='partial'
    receipt=saved(tmp_path,p);receipt['scene_id']='metadata-missing-000'
    with pytest.raises(ValueError,match='scene'):
        verify_delivery({'plan':p,'deliveries':[receipt]})
    receipt=saved(tmp_path,p);receipt['sha256']='changed'
    with pytest.raises(ValueError,match='SHA'):
        verify_delivery({'plan':p,'deliveries':[receipt]})


def test_single_scene_sampler_callback_and_selection_no_extra_source(monkeypatch):
    import hpgee.planning.observations as observations
    called=[]
    class Image:
        def set(self,props): called.append(props);return self
    monkeypatch.setattr(observations,'observation_image',lambda scene,**kwargs:Image())
    import ee
    monkeypatch.setattr(ee.ImageCollection,'fromImages',lambda images:images)
    p=plan();unit=p['units'][0]
    args=sampler_arguments(unit)
    assert args['selection'].max_scenes==1 and args['selection'].mode=='all'
    assert len(args['collection_builder'](args['records'][0]))==1
    assert called[-1]['system:index']==unit['scene']['system_index']
    assert args['grid_builder'](None,None)==unit['pixel_grid']


def test_frozen_observation_uses_real_engine_without_metadata_reads(tmp_path, monkeypatch):
    import ee
    from ee import apitestcase
    from pathlib import Path
    from hpgee.core.models import EECUMonitorConfig, RunConfig
    from hpgee.transfer.engine import DownloadEngine

    helper = apitestcase.ApiTestCase(); helper.setUp()
    try:
        unit = plan()['units'][0]
        payload = Path(saved(tmp_path, {'units': [unit]})['path']).read_bytes()
        def forbidden(*args, **kwargs):
            pytest.fail('frozen observation must not request metadata')
        for name in ('computeValue', 'computeFeatures', 'listImages', 'getAsset', 'getInfo', 'getList'):
            monkeypatch.setattr(ee.data, name, forbidden)
        requests = []
        def compute(request):
            requests.append(request)
            assert request['bandIds'] == BANDS
            assert request['grid'] == unit['pixel_grid']
            return payload
        monkeypatch.setattr(ee.data, 'computePixels', compute)
        engine = DownloadEngine('test-project', RunConfig(
            tmp_path / 'hot', workers=1, retries=0, eecu=EECUMonitorConfig(enabled=False)),
            ee_module=ee)
        arguments = sampler_arguments(unit)
        summary = engine.download_patch_series(**arguments, run_id='frozen')
        assert summary.succeeded == 1 and summary.failed == 0
        result = summary.results[0]
        assert result.scene_id == unit['scene']['system_index']
        assert result.scene_date == '2025-12-20T00:00:00+00:00'
        assert result.sample_properties['original_source'] == unit['scene']['asset_id']
        resumed = engine.download_patch_series(**arguments, run_id='frozen')
        assert resumed.skipped == 1 and resumed.bytes_downloaded == 0
        assert len(requests) == 1
    finally:
        helper.tearDown(); ee.Reset()


def test_frozen_observation_grid_is_snapshotted_without_mutating_the_plan():
    unit = plan()['units'][0]
    expected = deepcopy(unit['pixel_grid'])
    arguments = sampler_arguments(unit)
    arguments['grid_builder'](None, None)['dimensions']['width'] = 99
    assert unit['pixel_grid'] == expected
    unit['pixel_grid']['dimensions']['height'] = 99
    assert arguments['grid_builder'](None, None) == expected


def test_frozen_observation_expression_keeps_original_source_and_grid():
    import ee
    from ee import apitestcase
    helper = apitestcase.ApiTestCase(); helper.setUp()
    try:
        unit = plan()['units'][0]
        arguments = sampler_arguments(unit)
        sample = arguments['records'][0]
        expression = arguments['collection_builder'](sample).serialize()
        unit['scene']['system_index'] = 'different-source'
        unit['scene']['asset_id'] = unit['scene']['collection'] + '/different-source'
        unit['grid']['transform'][2] += 30
        assert arguments['collection_builder'](sample).serialize() == expression
    finally:
        helper.tearDown(); ee.Reset()


def test_adapter_replay_preserves_bytes_and_trial_identity(tmp_path):
    p=plan();receipt=saved(tmp_path,p);unit=p['units'][0]
    calls=[]
    class Engine:
        config=N(output_dir=tmp_path/'hot',workload_prefix='test')
        def download_patch_series(self,**kwargs):
            calls.append(kwargs)
            return N(workload_tag='sampler-actual-tag',results=[N(status='success',output_path=receipt['path'],scene_id=unit['scene']['system_index'],scene_date='2025-12-20T00:00:00+00:00',bytes_downloaded=100)],failed=0)
    kwargs=dict(engine=Engine(),hot_dir=tmp_path/'hot',archive_dir=tmp_path/'archive',trial_id='trial1')
    a=deliver_unit(unit,**kwargs);b=deliver_unit(unit,**kwargs)
    assert len(calls)==1 and b['local_cache_hit'] is True
    assert a['workload_tag']=='sampler-actual-tag' and a['network_bytes']==100
    deliver_unit(unit,**dict(kwargs,trial_id='trial2'))
    assert len(calls)==2
    from pathlib import Path
    Path(a['path']).write_bytes(b'corruption')
    with pytest.raises(ValueError,match='SHA'):
        deliver_unit(unit,**kwargs)


def test_all_invalid_pixels_remain_full_draw_unknown(tmp_path):
    p=plan();values=np.full((9,2,2),np.nan,dtype='float32');values[7]=0;values[8]=-1
    receipt=saved(tmp_path,p,values)
    result=verify_delivery({'plan':p,'deliveries':[receipt]})
    assert result['status']=='complete' and result['supports'][0]['requested_pixels']==4
    assert result['supports'][0]['observed_pixels']==0


def test_claimed_valid_source_band_missing_saved_values_rejected(tmp_path):
    p=plan();values=np.ones((9,2,2),dtype='float32');values[6]=0;values[7]=127;values[8]=1;values[0,0,0]=np.nan
    receipt=saved(tmp_path,p,values)
    with pytest.raises(ValueError,match='validity bit'):
        verify_delivery({'plan':p,'deliveries':[receipt]})


def test_s1_requires_common_support_bits_and_valid_fraction(tmp_path):
    p=plan();unit=p['units'][0];unit['scene']['modality']='S1'
    unit['bands']=[dict(name=name,dtype='float32',unit='native',recipe='s1-common-area-power-v1') for name in ('VV_power','VH_power','angle','source_valid_bits','valid_fraction')]
    values=np.ones((5,2,2),dtype='float32');values[3]=5;values[4]=0
    path=tmp_path/'s1.tif'
    with rasterio.open(path,'w',driver='GTiff',width=2,height=2,count=5,dtype='float32',crs='EPSG:32635',transform=Affine(30,0,500000,0,-30,1000000),nodata=np.nan) as dst: dst.write(values)
    receipt=dict(unit_id=unit['unit_id'],path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),status='verified',scene_id=unit['scene']['system_index'],scene_time_ms=unit['scene']['time_ms'],band_order=[b['name'] for b in unit['bands']])
    with pytest.raises(ValueError,match='common support'):
        verify_delivery({'plan':p,'deliveries':[receipt]})


def test_archive_receipt_cannot_redirect_to_different_valid_file(tmp_path):
    p=plan();receipt=saved(tmp_path,p);unit=p['units'][0]
    class Engine:
        config=N(output_dir=tmp_path/'hot',workload_prefix='test')
        def download_patch_series(self,**kwargs):
            return N(workload_tag='actual',results=[N(status='success',output_path=receipt['path'],scene_id=unit['scene']['system_index'],scene_date='2025-12-20T00:00:00Z',bytes_downloaded=100)],failed=0)
    kw=dict(engine=Engine(),hot_dir=tmp_path/'hot',archive_dir=tmp_path/'archive',trial_id='trial')
    result=deliver_unit(unit,**kw)
    from pathlib import Path
    path=Path(result['path']).with_suffix('.json');doc=json.loads(path.read_text());doc['path']=receipt['path'];path.write_text(json.dumps(doc))
    with pytest.raises(ValueError,match='receipt path'):
        deliver_unit(unit,**kw)


def test_sampler_rejects_contract_that_lazy_observation_recipe_cannot_produce():
    unit=plan()['units'][0]
    unit['bands'][0]['dtype']='float64'
    with pytest.raises(ValueError,match='observation contract'):
        sampler_arguments(unit)


def test_sampler_does_not_silently_run_different_recipe():
    unit=plan()['units'][0]
    unit['bands'][0]['recipe']='new-unimplemented-resampling'
    with pytest.raises(ValueError,match='recipe'):
        sampler_arguments(unit)


def tag_engine(tmp_path,unit,*,prefix='123456789ab'):
    """Fake only the network boundary; use real saved bytes and sampler tags."""
    from hpgee.transfer.engine import make_workload_tag
    source=saved(tmp_path,{'units':[unit]})['path']
    class Engine:
        config=N(output_dir=tmp_path/'hot',workload_prefix=prefix)
        calls=[]
        def download_patch_series(self,**kwargs):
            self.calls.append(kwargs)
            tag=make_workload_tag(self.config.workload_prefix,kwargs['scenario'],kwargs['run_id'])
            return N(workload_tag=tag,results=[N(status='success',output_path=source,
                scene_id=unit['scene']['system_index'],scene_date='2025-12-20T00:00:00Z',bytes_downloaded=100)],failed=0)
    return Engine()


def test_long_trial_keeps_full_unit_identity_in_real_sampler_tag(tmp_path):
    import re
    unit=plan()['units'][0];engine=tag_engine(tmp_path,unit)
    kw=dict(engine=engine,hot_dir=tmp_path/'hot',archive_dir=tmp_path/'archive',trial_id='legal-long-trial-'+'a'*100)
    first=deliver_unit(unit,**kw)
    other=deepcopy(unit);other['unit_id']='b'*64
    second=deliver_unit(other,**kw)
    assert first['workload_tag']!=second['workload_tag']
    for receipt in (first,second):
        assert re.fullmatch(r'[a-f0-9]{32}',receipt['sampler_run_id'])
        assert receipt['sampler_run_id'] in receipt['workload_tag']
        assert re.fullmatch(r'[a-z0-9_-]{1,63}',receipt['workload_tag'])
        assert receipt['request_identity']['archive_dir']==str((tmp_path/'archive').resolve())


def test_same_unit_separate_archive_runs_never_share_sampler_tag(tmp_path):
    unit=plan()['units'][0];engine=tag_engine(tmp_path,unit)
    kw=dict(engine=engine,hot_dir=tmp_path/'hot',trial_id='first')
    first=deliver_unit(unit,archive_dir=tmp_path/'run-one',**kw)
    second=deliver_unit(unit,archive_dir=tmp_path/'run-two',**kw)
    assert first['workload_tag']!=second['workload_tag']
    assert len(engine.calls)==2
    replay=deliver_unit(unit,archive_dir=tmp_path/'run-one',**kw)
    assert replay['sampler_run_id']==first['sampler_run_id'] and replay['network_bytes']==0
    assert len(engine.calls)==2


def test_prefix_that_truncates_request_identity_rejected_before_network(tmp_path):
    unit=plan()['units'][0];engine=tag_engine(tmp_path,unit,prefix='x'*12)
    with pytest.raises(ValueError,match='workload.*identity'):
        deliver_unit(unit,engine=engine,hot_dir=tmp_path/'hot',archive_dir=tmp_path/'archive',trial_id='first')
    assert engine.calls==[]


def test_legacy_receipt_replay_does_not_require_new_identity_or_tag_prefix(tmp_path):
    from pathlib import Path
    unit=plan()['units'][0];engine=tag_engine(tmp_path,unit)
    kw=dict(engine=engine,hot_dir=tmp_path/'hot',archive_dir=tmp_path/'archive',trial_id='first')
    first=deliver_unit(unit,**kw)
    receipt_path=Path(first['path']).with_suffix('.json')
    legacy=json.loads(receipt_path.read_text())
    legacy.pop('sampler_run_id',None);legacy.pop('request_identity',None)
    receipt_path.write_text(json.dumps(legacy))
    engine.config.workload_prefix='old-overlong-prefix'
    replay=deliver_unit(unit,**kw)
    assert replay['sha256']==first['sha256'] and replay['network_bytes']==0
    assert len(engine.calls)==1 and 'sampler_run_id' not in replay
