import json
import pytest
import ee
from ee import apitestcase
from hpgee.core.models import SampleRecord, PatchGrid
from hpgee.workflows.config import Config
from hpgee.compute.features import hls_collection, min_nbr2_composite, feature_image, s1_power_composite, dem_image
from hpgee.workflows.recipe import hls_scene_collection, frozen_grid
from .test_contract import configuration


@pytest.fixture(autouse=True)
def offline_ee():
    # Bundled upstream algorithms; disable transport initialization. No credential loading or network.
    helper=apitestcase.ApiTestCase(); helper.setUp()
    yield
    helper.tearDown(); ee.Reset()


def test_real_ee_graph_serializes_hls_rf_and_optional_features(tmp_path):
    raw=configuration(); raw['s1']={'relative_orbit':34,'pass':'DESCENDING'}
    cfg=Config.from_dict(raw)
    for graph in [hls_collection(cfg),min_nbr2_composite(hls_collection(cfg),cfg),feature_image(cfg),
                  s1_power_composite(cfg),dem_image(cfg)]:
        serialized=json.loads(graph.serialize())
        assert serialized['result'] and serialized['values']
    path=tmp_path/'cfg.json'; path.write_text(json.dumps(raw))
    sample=SampleRecord('roi',{'type':'Point','coordinates':[30.02,-1.97]})
    assert hls_scene_collection(sample,config_path=path).serialize()
    grid=frozen_grid(sample,PatchGrid(size=128,scale=30),config_path=path)
    assert grid['dimensions']=={'width':128,'height':128}
    assert grid['crsCode']=='EPSG:32736'


def test_rf_graph_contract_without_server_training(tmp_path,monkeypatch):
    from hpgee.compute.rf import train_rf
    from .test_contract import row
    manifest=tmp_path/'split.json'
    manifest.write_text(json.dumps({'events':{'one':'train'},'supports':{'one':'train'}}))
    raw=configuration(); raw['labels']={'frozen_split_manifest':str(manifest),'max_samples':5}
    cfg=Config.from_dict(raw)
    records={'features':[{'type':'Feature','geometry':None,'properties':row(0)},
                         {'type':'Feature','geometry':None,'properties':row(1)}]}
    # Mock only the external sample read; real ee client builds model/classification graphs.
    monkeypatch.setattr(ee.data,'computeValue',lambda graph:records['features'])
    model,scores,metadata=train_rf(ee.FeatureCollection('projects/example/assets/samples'),cfg)
    assert json.loads(model.serialize())['result']
    assert json.loads(scores.serialize())['result']
    assert json.loads(model.explain().select(['importance','numberOfTrees']).serialize())['result']
    assert metadata['class_order']==[0,1] and metadata['score_calibrated'] is False
    assert len(metadata['frozen_split_manifest_sha256'])==64


def test_rf_variants_freeze_identical_cohort_and_dem_graph(tmp_path,monkeypatch):
    from dataclasses import replace
    from hpgee.compute.rf import train_rf
    from hpgee.compute.features import RF_BANDS, DEM_BANDS, prepare_hls
    from .test_contract import row
    split=tmp_path/'split.json'; split.write_text(json.dumps({'events':{'one':'train'},'supports':{'one':'train'}}))
    raw=configuration(); raw['labels']={'frozen_split_manifest':str(split),'max_samples':5}
    cfg=Config.from_dict(raw)
    records={'features':[{'type':'Feature','geometry':None,'properties':dict(row(label),**dict.fromkeys(DEM_BANDS,.1))} for label in (0,1)]}
    monkeypatch.setattr(ee.data,'computeValue',lambda graph:records['features'])
    hashes=[]
    for trees in (50,100,200):
        for feature_set in ('core6','core6_dem'):
            model,scores,metadata=train_rf(ee.FeatureCollection('projects/example/assets/samples'),
                replace(cfg,rf={'trees':trees,'feature_set':feature_set}),snapshot_bands=RF_BANDS+DEM_BANDS)
            assert metadata['trees']==trees
            assert len(metadata['features'])==(10 if feature_set=='core6_dem' else 6)
            assert json.loads(scores.serialize())['result']
            hashes.append(metadata['sample_snapshot_sha256'])
    assert len(set(hashes))==1
    source=ee.Image('NASA/HLS/HLSL30/v002/EXAMPLE').set('system:time_start',1596240000123)
    graph=json.loads(prepare_hls(source,'L30').serialize())
    def invocations(value):
        if isinstance(value,dict):
            if 'functionInvocationValue' in value: yield value['functionInvocationValue']
            for child in value.values(): yield from invocations(child)
        elif isinstance(value,list):
            for child in value: yield from invocations(child)
    def function_name(value):
        if 'valueReference' in value: value=graph['values'][value['valueReference']]
        return value.get('functionInvocationValue',{}).get('functionName')
    # The final whole multiband image, rather than just its time band, must cast.
    assert any(inv['functionName']=='Image.toDouble' and function_name(inv['arguments']['value'])=='Image.addBands'
               for inv in invocations(graph))


def test_rf_training_graph_uses_validated_literal_snapshot_not_mutable_asset(tmp_path,monkeypatch):
    from hpgee.compute.rf import train_rf
    from .test_contract import row
    split=tmp_path/'split.json'; split.write_text(json.dumps({'events':{'one':'train'},'supports':{'one':'train'}}))
    raw=configuration(); raw['labels']={'frozen_split_manifest':str(split),'max_samples':5}
    captured={'features':[{'type':'Feature','geometry':None,'properties':row(label)} for label in (0,1)]}
    monkeypatch.setattr(ee.data,'computeValue',lambda graph:captured['features'])
    asset='projects/example/assets/MUTABLE_SOURCE'
    model,_,_=train_rf(ee.FeatureCollection(asset),Config.from_dict(raw))
    graph=model.serialize()
    assert asset not in graph
    assert 'FeatureCollection.loadTable' not in graph
    assert 'Feature' in graph and 'label_eligible' in graph


def test_local_input_helper_is_literal_and_drops_geometry(tmp_path):
    from hpgee.compute.rf import input_samples
    from .test_contract import row
    path=tmp_path/'local.json'
    path.write_text(json.dumps({'type':'FeatureCollection','features':[
        {'type':'Feature','geometry':{'type':'Point','coordinates':[30.0123456789,-1.987654321]},'properties':row(0)},
        {'type':'Feature','geometry':None,'properties':row(1)}]}))
    raw=configuration(); raw['labels']={'local_samples':str(path),'max_samples':2}
    samples,provenance=input_samples(Config.from_dict(raw))
    assert provenance['kind']=='local_feature_collection' and provenance['sample_count']==2
    graph=samples.serialize()
    assert 'FeatureCollection.loadTable' not in graph
    assert '30.0123456789' not in graph
    assert 'label_eligible' in graph


def test_local_rf_validation_uses_frozen_rows_without_remote_roundtrip(tmp_path, monkeypatch):
    import hashlib
    from hpgee.compute.rf import input_samples, train_rf
    from .test_contract import row
    split = tmp_path / 'split.json'
    split.write_text(json.dumps({'events': {'one': 'train'}, 'supports': {'one': 'train'}}))
    rows = [row(0), row(1)]
    samples_path = tmp_path / 'local.json'
    samples_path.write_text(json.dumps({'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'geometry': None, 'properties': item} for item in rows]}))
    raw = configuration()
    raw['labels'] = {'local_samples': str(samples_path),
                     'frozen_split_manifest': str(split), 'max_samples': 2}
    cfg = Config.from_dict(raw)
    samples, _ = input_samples(cfg)
    monkeypatch.setattr(ee.data, 'computeValue',
                        lambda graph: pytest.fail('local validation must not fetch its own rows from EE'))
    model, _, metadata = train_rf(samples, cfg)
    canonical = sorted(json.dumps(item, sort_keys=True, separators=(',', ':'),
                                  allow_nan=False) for item in rows)
    expected = hashlib.sha256(json.dumps(canonical, separators=(',', ':')).encode()).hexdigest()
    assert metadata['sample_snapshot_sha256'] == expected
    assert metadata['sample_count'] == 2
    assert 'FeatureCollection.loadTable' not in model.serialize()


def test_rf_bounded_read_above_collection_display_limit(tmp_path,monkeypatch):
    from hpgee.compute.rf import train_rf
    from .test_contract import row
    split=tmp_path/'split.json'; split.write_text(json.dumps({'events':{'one':'train'},'supports':{'one':'train'}}))
    raw=configuration(); raw['labels']={'frozen_split_manifest':str(split),'max_samples':8000}
    records=[{'type':'Feature','geometry':None,'properties':row(i%2)} for i in range(8000)]
    def rpc(graph):
        if isinstance(graph,ee.FeatureCollection):
            raise RuntimeError('Collection query aborted after accumulating over 5000 elements.')
        assert isinstance(graph,ee.List)
        assert '8001' in graph.serialize()
        return records
    monkeypatch.setattr(ee.data,'computeValue',rpc)
    _,_,metadata=train_rf(ee.FeatureCollection('projects/example/assets/samples'),Config.from_dict(raw))
    assert metadata['sample_count']==8000
