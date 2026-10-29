import json
import numpy as np
import pytest
import ee
from ee import apitestcase
from geecomputation.config import Config, utc_datetime
from .test_contract import configuration


def test_candidate_mask_preserves_unknown_and_threshold():
    from geecomputation.workflow import candidate_pixels
    scores=np.array([.49,.5,.9,np.nan,.8])
    valid=np.array([True,True,False,True,True])
    result=candidate_pixels(scores,valid,.5)
    assert result.mask.tolist()==[False,False,True,True,False]
    assert result.compressed().tolist()==[0,1,1]
    with pytest.raises(ValueError): candidate_pixels(scores,valid,float('nan'))


def test_contract_freezes_grid_aux_and_threshold():
    from geecomputation.workflow import workflow_contract
    cfg=Config.from_dict(configuration())
    a=workflow_contract(cfg,threshold=.5); b=workflow_contract(cfg,threshold=.6)
    assert a['contract_sha256']!=b['contract_sha256']
    assert a['grid']==cfg.pixel_grid()
    assert a['rf_inputs']==list(__import__('geecomputation.features',fromlist=['RF_BANDS']).RF_BANDS)
    assert a['aux']['DEM']['enters_rf'] is False
    assert 'pre_time_ms' in a['raster_bands'] and 'post_valid_count' in a['raster_bands']
    raw=configuration();raw['rf']={'feature_set':'core6_dem'}
    with pytest.raises(ValueError,match='core6'): workflow_contract(Config.from_dict(raw))


def test_partial_artifacts_cannot_pass_acceptance(tmp_path):
    from geecomputation.workflow import verify_artifacts,workflow_contract
    cfg=Config.from_dict(configuration());contract=workflow_contract(cfg)
    with pytest.raises(ValueError,match='raster'): verify_artifacts(tmp_path/'missing.tif',tmp_path/'missing.geojson',contract)
    path=tmp_path/'partial.tif';path.write_bytes(b'partial')
    with pytest.raises(ValueError,match='boundary'): verify_artifacts(path,tmp_path/'missing.geojson',contract)


def test_finalize_geesampler_tiff_without_band_descriptions(tmp_path):
    import rasterio
    from geecomputation.workflow import finalize_transfer
    raster,_,contract=saved_products(tmp_path)
    with rasterio.open(raster,'r+') as ds:
        original=ds.read();ds.descriptions=(None,)*ds.count
    destination=tmp_path/'final.tif'
    receipt=finalize_transfer(raster,destination,contract,transfer_bands=contract['raster_bands'])
    with rasterio.open(destination) as ds:
        assert list(ds.descriptions)==contract['raster_bands']
        assert np.array_equal(ds.read(),original)
    assert receipt['band_identity_source']=='frozen computePixels bandIds order'


def test_finalize_rejects_changed_transfer_band_order(tmp_path):
    from geecomputation.workflow import finalize_transfer
    raster,_,contract=saved_products(tmp_path)
    bands=list(contract['raster_bands']);bands[0],bands[1]=bands[1],bands[0]
    with pytest.raises(ValueError,match='bandIds'):
        finalize_transfer(raster,tmp_path/'final.tif',contract,transfer_bands=bands)


@pytest.fixture
def offline_ee():
    helper=apitestcase.ApiTestCase();helper.setUp()
    yield
    helper.tearDown();ee.Reset()


def test_product_graph_masks_candidate_and_vectors_on_output_grid(offline_ee):
    from geecomputation.workflow import build_products,workflow_contract,prepare_exports
    cfg=Config.from_dict(configuration())
    scores=ee.Image.constant([.2,.8]).rename(['score_unburned','score_burned'])
    products=build_products(cfg,scores,threshold=.5)
    graph=products['candidate'].serialize()
    assert 'Image.updateMask' in graph and 'Image.gte' in graph
    vectors=products['boundaries'].serialize()
    assert 'Image.reduceToVectors' in vectors and 'EPSG:32736' in vectors and '9785000' in vectors
    tasks=prepare_exports(cfg,products,asset_id='projects/example/assets/product',drive_folder='test')
    assert set(tasks)=={'image','table'}
    assert workflow_contract(cfg)['raster_bands']


def test_explicit_aux_context_uses_same_frozen_image_and_preserves_default_contract(offline_ee,monkeypatch):
    from geecomputation import workflow
    cfg=Config.from_dict(configuration());before=workflow.workflow_contract(cfg)
    aux=ee.Image.constant([1,2,3,4]).rename(['elevation','slope','aspect_sin','aspect_cos'])
    monkeypatch.setattr(workflow,'dem_image',lambda cfg:pytest.fail('frozen aux must bypass dynamic DEM selection'))
    scores=ee.Image.constant([.2,.8]).rename(['score_unburned','score_burned'])
    p=workflow.build_products(cfg,scores,aux=aux,aux_context_sha256='a'*64)
    assert p['contract']['aux']['DEM']['selection']=='caller_frozen_auxiliary_context'
    assert p['contract']['aux_context_sha256']=='a'*64
    assert p['contract']['contract_sha256']!=before['contract_sha256']
    assert 'COPERNICUS/DEM/GLO30' not in p['aux'].serialize()
    assert workflow.workflow_contract(cfg)==before


def test_caller_auxiliary_context_does_not_claim_default_dem_collection():
    from geecomputation.workflow import workflow_contract
    from geecomputation.features import DEM_COLLECTION
    cfg=Config.from_dict(configuration())
    default=workflow_contract(cfg)
    explicit=workflow_contract(cfg,aux_context_sha256='a'*64)
    assert default['aux']['DEM']['collection']==DEM_COLLECTION
    assert explicit['aux']['DEM']['collection'] is None
    assert explicit['aux']['DEM']['native_support']=='caller-declared in frozen auxiliary context'
    assert explicit['aux_context_sha256']=='a'*64
    assert explicit['aux']['DEM']['enters_rf'] is False
    assert workflow_contract(cfg)==default


@pytest.mark.parametrize('kwargs',[{'aux_context_sha256':'a'*64},{'aux':1},
    {'aux':1,'aux_context_sha256':'bad'},{'aux':1,'aux_context_sha256':True}])
def test_auxiliary_context_requires_both_explicit_image_and_sha(offline_ee,kwargs):
    from geecomputation.workflow import build_products
    with pytest.raises(ValueError,match='aux'):
        build_products(Config.from_dict(configuration()),ee.Image.constant([.2,.8]),**kwargs)


def test_pixel_qa_can_accept_explicit_no_observation_but_not_false_unburned(tmp_path):
    import rasterio
    from geecomputation.workflow import verify_pixels,verify_artifacts
    raster,boundary,contract=saved_products(tmp_path)
    assert verify_pixels(raster,contract)['observed_pixels']==1
    with rasterio.open(raster,'r+') as ds:
        data=ds.read();data[:9]=-9999;ds.write(data)
    result=verify_pixels(raster,contract,allow_no_observation=True)
    assert result['status']=='no_observation' and result['candidate_pixels']==0 and result['unknown_pixels']==2
    with pytest.raises(ValueError,match='no observed'):verify_pixels(raster,contract)
    with pytest.raises(ValueError,match='no observed'):verify_artifacts(raster,boundary,contract)
    with rasterio.open(raster,'r+') as ds:
        data=ds.read();data[contract['raster_bands'].index('candidate')]=0;ds.write(data)
    with pytest.raises(ValueError,match='mask'):verify_pixels(raster,contract,allow_no_observation=True)


def test_pixel_qa_rejects_bad_source_clock_even_without_vector_check(tmp_path):
    import rasterio
    from geecomputation.workflow import verify_pixels
    raster,_,contract=saved_products(tmp_path)
    with rasterio.open(raster,'r+') as ds:
        data=ds.read();data[contract['raster_bands'].index('pre_time_ms'),0,0]=0;ds.write(data)
    with pytest.raises(ValueError,match='time'):verify_pixels(raster,contract)


@pytest.mark.parametrize('flag',['false',1,None,[],{}])
def test_no_observation_requires_explicit_boolean_opt_in(tmp_path,flag):
    from geecomputation.workflow import verify_pixels
    raster,_,contract=saved_products(tmp_path)
    with pytest.raises(ValueError,match='bool'):verify_pixels(raster,contract,allow_no_observation=flag)


def test_sampling_preserves_source_properties_and_uses_fixed_projection(offline_ee,tmp_path):
    from geecomputation.workflow import sample_points
    points=tmp_path/'points.geojson'
    points.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','geometry':{'type':'Point','coordinates':[30.01,-1.98]},'properties':{'label':1,'event_id':'e','support_id':'s','split':'train','label_source':'weak','label_eligible':True,'source_version':'NBAC2020','source_id':'agency-1'}}]}))
    samples,source=sample_points(Config.from_dict(configuration()),points)
    graph=samples.serialize()
    assert 'Image.sampleRegions' in graph and 'source_version' in graph and 'NBAC2020' in graph
    assert 'EPSG:32736' in graph and '9785000' in graph
    assert source['point_count']==1 and len(source['sha256'])==64


def test_sampling_accepts_actual_ee_point_roundtrip(offline_ee,tmp_path):
    from geecomputation.workflow import sample_points
    points=tmp_path/'points.geojson'
    points.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature',
        'geometry':{'type':'Point','coordinates':[-110.69097688980801,59.05641604117655],'geodesic':False},
        'properties':{'label':1,'event_id':'e','support_id':'s','split':'train','label_source':'weak','label_eligible':True}}]}))
    samples,_=sample_points(Config.from_dict(configuration()),points)
    assert 'Image.sampleRegions' in samples.serialize()


def saved_products(tmp_path,*,partial=False):
    import rasterio
    from rasterio.transform import Affine
    from rasterio.warp import transform_geom
    from geecomputation.workflow import workflow_contract
    raw=configuration();raw['grid']['width']=2;raw['grid']['height']=1
    contract=workflow_contract(Config.from_dict(raw));bands=contract['raster_bands']
    a=np.full((len(bands),1,2),-9999.,dtype='float64');idx={b:i for i,b in enumerate(bands)}
    for b in contract['rf_inputs']: a[idx[b],0,0]=.1
    a[idx['score_burned'],0,0]=.8;a[idx['score_unburned'],0,0]=.2;a[idx['candidate'],0,0]=1
    for period in ('pre','post'):
        a[idx[period+'_valid_count'],0]=[2,0]
        a[idx[period+'_time_ms'],0,0]=int(utc_datetime(raw['pre_start' if period=='pre' else 'start']).timestamp()*1000)
        a[idx[period+'_Fmask'],0,0]=0
        a[idx[period+'_clear'],0,0]=1
    transform=Affine(*raw['grid']['transform']);raster=tmp_path/'product.tif'
    with rasterio.open(raster,'w',driver='GTiff',width=2,height=1,count=len(bands),dtype='float64',crs=raw['grid']['crs'],transform=transform,nodata=-9999.) as ds:
        ds.write(a);ds.descriptions=tuple(bands)
    x,y=transform.c,transform.f
    polygon={'type':'Polygon','coordinates':[[[x,y],[x+30,y],[x+30,y-30],[x,y-30],[x,y]]]}
    f={'type':'Feature','properties':{'candidate':1,'threshold':.5},'geometry':transform_geom(raw['grid']['crs'],'EPSG:4326',polygon)}
    boundary=tmp_path/'boundary.geojson';boundary.write_text(json.dumps({'type':'FeatureCollection','features':[] if partial else [f]}))
    return raster,boundary,contract


def test_saved_pixel_acceptance_allows_independently_missing_aux(tmp_path):
    from geecomputation.workflow import verify_artifacts
    raster,boundary,contract=saved_products(tmp_path)
    result=verify_artifacts(raster,boundary,contract)
    assert result['observed_pixels']==1 and result['candidate_pixels']==1
    assert result['independent_accuracy_acceptance'] is False


def test_saved_raster_boundaries_preserve_candidate_union_without_ee(tmp_path, monkeypatch):
    from geecomputation.workflow import boundaries_from_saved_raster, verify_artifacts
    raster, _, contract = saved_products(tmp_path)
    monkeypatch.setattr(ee.data, 'computeValue',
                        lambda *_: pytest.fail('saved-mask polygonization must stay local'))
    geojson = boundaries_from_saved_raster(raster, threshold=contract['threshold'])
    assert len(geojson['features']) == 1
    assert geojson['features'][0]['properties'] == {'candidate': 1, 'threshold': .5}
    boundary = tmp_path / 'local-boundaries.geojson'
    boundary.write_text(json.dumps(geojson))
    assert verify_artifacts(raster, boundary, contract)['candidate_pixels'] == 1


def test_saved_raster_boundaries_use_eight_connectivity_and_cap(tmp_path):
    import rasterio
    from rasterio.transform import from_origin
    from geecomputation.workflow import boundaries_from_saved_raster
    raster = tmp_path / 'candidate.tif'
    with rasterio.open(raster, 'w', driver='GTiff', width=3, height=3, count=1,
                       dtype='uint8', crs='EPSG:32612',
                       transform=from_origin(500000, 6500000, 30, 30)) as ds:
        ds.write(np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype='uint8'), 1)
        ds.descriptions = ('candidate',)
    assert len(boundaries_from_saved_raster(raster, threshold=.5, cap=1)['features']) == 1
    with rasterio.open(raster, 'r+') as ds:
        ds.write(np.array([[1, 0, 1], [0, 0, 0], [0, 0, 0]], dtype='uint8'), 1)
    with pytest.raises(ValueError, match='cap exceeded'):
        boundaries_from_saved_raster(raster, threshold=.5, cap=1)


def test_transfer_workflow_does_not_evaluate_ee_boundaries(tmp_path, offline_ee, monkeypatch):
    from contextlib import nullcontext
    from geesampler.models import RunSummary, TaskResult
    from geecomputation import cli, workflow
    from geesampler import engine
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    source, _, _ = saved_products(source_dir)
    raw = configuration()
    raw['grid'].update(width=2, height=1)
    raw['output'] = str(tmp_path / 'output')
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(raw))

    class FakeEngine:
        def __init__(self, *args, **kwargs):
            pass

        def download_patch_series(self, *args, **kwargs):
            return RunSummary('test', 'test-tag', 1, 1, 0, 0, source.stat().st_size,
                              1., 1., 1., results=(TaskResult('task', 'workflow', 'success',
                                  str(source), source.stat().st_size, 1., 1),))

    timeouts = []
    def initialize_for_transfer(cfg, *, timeout):
        timeouts.append(timeout)
        return ee
    monkeypatch.setattr(cli, 'initialize', initialize_for_transfer)
    monkeypatch.setattr(ee, 'profilePrinting', lambda **kwargs: nullcontext())
    monkeypatch.setattr(ee.data, 'computeValue',
                        lambda *_: pytest.fail('transfer must not evaluate EE boundaries'))
    monkeypatch.setattr(engine, 'DownloadEngine', FakeEngine)
    monkeypatch.setattr(workflow, 'build_workflow',
                        lambda cfg, threshold: {'raster': ee.Image.constant(0),
                            'boundaries': object(), 'contract': workflow.workflow_contract(cfg, threshold),
                            'bands': list(workflow.PRODUCT_BANDS), 'metadata': {}})
    assert workflow.main(['--config', str(config_path), '--transfer',
                          '--hot-dir', str(tmp_path / 'hot')]) == 0
    assert timeouts == [330]
    output = tmp_path / 'output'
    assert json.loads((output / 'verification.json').read_text())['status'] == 'verified_saved_products'
    assert [row['stage'] for row in json.loads((output / 'stages.json').read_text())['stages']] == [
        'feature-train-graph', 'transfer', 'boundary-from-saved-raster', 'verify']


def test_missing_boundary_piece_rejected_despite_successful_raster(tmp_path):
    from geecomputation.workflow import verify_artifacts
    raster,boundary,contract=saved_products(tmp_path,partial=True)
    with pytest.raises(ValueError,match='partial'): verify_artifacts(raster,boundary,contract)


def test_unknown_pixels_cannot_be_filled_as_unburned(tmp_path):
    import rasterio
    from geecomputation.workflow import verify_artifacts
    raster,boundary,contract=saved_products(tmp_path)
    with rasterio.open(raster,'r+') as ds:
        candidate=ds.read(3);candidate[0,1]=0;ds.write(candidate,3)
    with pytest.raises(ValueError,match='mask'): verify_artifacts(raster,boundary,contract)


@pytest.mark.parametrize('unburned',[9.,.5])
def test_invalid_binary_probability_rejected(tmp_path,unburned):
    import rasterio
    from geecomputation.workflow import verify_artifacts
    raster,boundary,contract=saved_products(tmp_path)
    with rasterio.open(raster,'r+') as ds:
        values=ds.read(1);values[0,0]=unburned;ds.write(values,1)
    with pytest.raises(ValueError,match='probabilit'):
        verify_artifacts(raster,boundary,contract)


def test_boundary_outside_output_grid_rejected(tmp_path):
    import rasterio
    from rasterio.warp import transform_geom
    from geecomputation.workflow import verify_artifacts
    raster,boundary,contract=saved_products(tmp_path)
    data=json.loads(boundary.read_text())
    with rasterio.open(raster) as ds:
        x,y=ds.transform.c+600,ds.transform.f
        polygon={'type':'Polygon','coordinates':[[[x,y],[x+30,y],[x+30,y-30],[x,y-30],[x,y]]]}
        data['features'].append({'type':'Feature','properties':{'candidate':1,'threshold':.5},
            'geometry':transform_geom(ds.crs,'EPSG:4326',polygon)})
    boundary.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='outside'):
        verify_artifacts(raster,boundary,contract)


@pytest.mark.parametrize('band,value',[
    ('pre_time_ms',-9999.),('pre_time_ms',1596240000000.),
    ('post_time_ms',1593561600000.),('pre_valid_count',2.5),
    ('pre_Fmask',2.),('pre_clear',0.),('post_Fmask',-9999.),('post_clear',-9999.)])
def test_invalid_observation_quality_rejected(tmp_path,band,value):
    import rasterio
    from geecomputation.workflow import verify_artifacts
    raster,boundary,contract=saved_products(tmp_path)
    index=contract['raster_bands'].index(band)+1
    with rasterio.open(raster,'r+') as ds:
        values=ds.read(index);values[0,0]=value;ds.write(values,index)
    with pytest.raises(ValueError,match='observation|quality'):
        verify_artifacts(raster,boundary,contract)


def test_boundary_overflow_rejected(offline_ee,monkeypatch):
    from geecomputation.workflow import bounded_boundaries
    monkeypatch.setattr(ee.data,'computeValue',lambda graph:[{},{}])
    with pytest.raises(ValueError,match='cap exceeded'): bounded_boundaries(ee.FeatureCollection([]),cap=1)


def test_point_input_hash_changes_contract(tmp_path):
    from geecomputation.workflow import workflow_contract
    cfg=Config.from_dict(configuration());path=tmp_path/'points.json';path.write_text('first')
    a=workflow_contract(cfg,sample_points_path=path)
    path.write_text('second');b=workflow_contract(cfg,sample_points_path=path)
    assert a['contract_sha256']!=b['contract_sha256']


def test_workflow_manifest_cli_offline(tmp_path,capsys,monkeypatch):
    from geecomputation.workflow import main
    from geecomputation import cli
    raw=configuration();raw['output']=str(tmp_path/'output')
    path=tmp_path/'cfg.json';path.write_text(json.dumps(raw))
    monkeypatch.setattr(cli,'initialize',lambda cfg:pytest.fail('offline manifest must not authenticate'))
    assert main(['--config',str(path),'--manifest-only'])==0
    assert json.loads(capsys.readouterr().out)['threshold']==.5
    assert (tmp_path/'output'/'workflow-contract.json').exists()


def test_stage_receipt_does_not_claim_pure_train_or_complete_cost(tmp_path,offline_ee,monkeypatch):
    from contextlib import nullcontext
    from geecomputation.workflow import StageRecorder
    monkeypatch.setattr(ee,'profilePrinting',lambda **kwargs:nullcontext())
    recorder=StageRecorder(Config.from_dict(configuration()),tmp_path)
    with recorder.stage('train-request',evaluates_graph=True) as row: row['payload_bytes']=100
    document=json.loads((tmp_path/'stages.json').read_text())
    assert document['sum_pure_train_seconds'] is None and document['eecu_seconds'] is None
    assert document['telemetry_complete'] is False
    assert document['stages'][0]['evaluates_lazy_graph'] is True
    assert document['stages'][0]['wall_seconds']>=0
    assert document['stages'][0]['payload_bytes']==100


def test_stage_usage_keeps_reported_progress_without_double_count(tmp_path,offline_ee,monkeypatch):
    from contextlib import nullcontext
    from geecomputation.workflow import StageRecorder
    from geecomputation import monitoring
    monkeypatch.setattr(ee,'profilePrinting',lambda **kwargs:nullcontext())
    monkeypatch.setattr(monitoring,'read_usage',lambda *args,**kwargs:{'completed_seconds':1.,
        'in_progress_seconds':3.,'guard_hours':3./3600,'status':'REPORTED'})
    recorder=StageRecorder(Config.from_dict(configuration()),tmp_path)
    with recorder.stage('request'): pass
    recorder.refresh_usage(end='2026-10-13T06:00:00Z')
    row=json.loads((tmp_path/'usage-observations.json').read_text())['stages'][0]
    assert row['reported_eecu_seconds']==3.
    assert row['telemetry_complete'] is False and row['eecu_seconds'] is None


def test_workflow_auxiliary_dem_never_enters_training_graph(offline_ee,tmp_path,monkeypatch):
    from geecomputation.workflow import build_workflow
    from geecomputation.features import RF_BANDS,DEM_BANDS
    from .test_contract import row
    split=tmp_path/'split.json';split.write_text(json.dumps({'events':{'one':'train'},'supports':{'one':'train'}}))
    raw=configuration();raw['labels']={'frozen_split_manifest':str(split),'max_samples':2}
    records=[{'type':'Feature','geometry':None,'properties':dict(row(label),**dict.fromkeys(DEM_BANDS,42.))} for label in (0,1)]
    monkeypatch.setattr(ee.data,'computeValue',lambda graph:records)
    products=build_workflow(Config.from_dict(raw),samples=ee.FeatureCollection([]))
    assert products['metadata']['features']==list(RF_BANDS)
    model=products['model'].serialize()
    encoded=json.loads(model)
    train=next(v['functionInvocationValue'] for v in encoded['values'].values()
               if v.get('functionInvocationValue',{}).get('functionName')=='Classifier.train')
    inputs=train['arguments']['inputProperties']
    if 'valueReference' in inputs: inputs=encoded['values'][inputs['valueReference']]
    assert inputs.get('constantValue')==list(RF_BANDS)
    assert 'aux_dem_elevation' in products['raster'].serialize()


def test_default_workflow_contract_retains_legacy_hash_and_source_fields():
    from geecomputation.workflow import workflow_contract
    contract=workflow_contract(Config.from_dict(configuration()))
    assert 'source_selection' not in contract and 'source_manifest_sha256' not in contract
    assert contract['contract_sha256']=='ae22e1f332bb74c7a06d34e5e2255f1cb66832a9b1f2d6ba237e013535805df7'


def test_frozen_source_manifest_changes_workflow_contract():
    from geecomputation.workflow import workflow_contract
    cfg=Config.from_dict(configuration())
    first=workflow_contract(cfg,source_manifest_sha256='a'*64)
    second=workflow_contract(cfg,source_manifest_sha256='b'*64)
    assert first['source_selection']=='frozen_original_scene_list'
    assert first['source_manifest_sha256']=='a'*64
    assert first['contract_sha256']!=second['contract_sha256']
    assert first['recipe']==workflow_contract(cfg)['recipe']


@pytest.mark.parametrize('digest',['short','g'*64,'a'*63,64,True])
def test_frozen_source_manifest_requires_sha256(digest):
    from geecomputation.workflow import workflow_contract
    with pytest.raises(ValueError,match='source_manifest_sha256'):
        workflow_contract(Config.from_dict(configuration()),source_manifest_sha256=digest)


@pytest.mark.parametrize('kwargs',[
    {'source_manifest_sha256':'a'*64},
    {'pre':object(),'source_manifest_sha256':'a'*64},
    {'post':object(),'source_manifest_sha256':'a'*64},
    {'pre':object(),'post':object()},
])
def test_frozen_composites_require_both_periods_and_manifest(kwargs):
    from geecomputation.workflow import build_products
    with pytest.raises(ValueError,match='pre.*post.*source_manifest_sha256'):
        build_products(Config.from_dict(configuration()),object(),**kwargs)


def test_frozen_composites_build_actual_graph_without_dynamic_scene_selection(offline_ee,monkeypatch):
    from geecomputation import workflow
    from geecomputation.features import rf_image
    cfg=Config.from_dict(configuration())
    bands=['nir','swir1','NBR2','MIRBI','valid_count','time_ms','Fmask','clear']
    pre=ee.Image.constant([.5,.6,.5,-1.88,2,1593561600000,0,1]).rename(bands)
    post=ee.Image.constant([.2,.2,-.5,6.04,3,1596240000000,0,1]).rename(bands)
    scores=ee.Image.constant([.2,.8]).rename(['score_unburned','score_burned'])
    def reject_dynamic(*args,**kwargs):
        raise AssertionError('frozen input must never query dynamic HLS sources')
    monkeypatch.setattr(workflow,'hls_collection',reject_dynamic)
    products=workflow.build_products(cfg,scores,pre=pre,post=post,source_manifest_sha256='a'*64)
    assert products['core6'].serialize()==rf_image(pre,post).serialize()
    graph=products['raster'].serialize()
    assert '1593561600000' in graph and '1596240000000' in graph
    assert 'pre_time_ms' in graph and 'post_time_ms' in graph
    assert products['contract']['source_manifest_sha256']=='a'*64
    assert products['contract']['candidate_meaning'].endswith('demonstration, not accepted burned area')
