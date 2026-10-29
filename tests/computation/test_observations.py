"""Offline listImages adapter fixtures and independent observation contracts."""
import json
from unittest.mock import Mock

import ee
from ee import apitestcase
import numpy as np
import pytest

from hpgee.planning.observations import HLS_OBSERVATION_BANDS, S1_OBSERVATION_BANDS, enumerate_observations, observation_query_plan, inspect_sources, observation_image, hls_observation_values, s1_observation_values

HLS = 'NASA/HLS/HLSL30/v002'
S1 = 'COPERNICUS/S1_GRD'
GRID = {'crs':'EPSG:32736', 'transform':[30,0,166000,0,-30,9785000]}
GEOMETRY = {'type':'Point','coordinates':[30,-2]}


@pytest.fixture
def offline_ee():
    helper = apitestcase.ApiTestCase(); helper.setUp()
    ee.data.setCloudApiUserProject('test-project')
    yield
    helper.tearDown(); ee.Reset()


def asset(index='one', collection=HLS, time='2020-08-01T00:00:00Z', **changes):
    bands = (['VV','VH','angle'] if collection == S1 else
             ['B2','B3','B4','B8A','B11','B12','Fmask'] if 'HLSS30' in collection else
             ['B2','B3','B4','B5','B6','B7','Fmask'])
    raw = {'name':f'projects/earthengine-public/assets/{collection}/{index}',
           'id':f'{collection}/{index}', 'type':'IMAGE', 'startTime':time,
           'properties':{'system:index':index},
           'bands':[{'id':band, 'grid':{'crsCode':'EPSG:32736',
               'dimensions':{'width':1000,'height':1000},
               'affineTransform':{'scaleX':10,'shearX':0,'translateX':166000,
                                  'shearY':0,'scaleY':-10,'translateY':9785000}}} for band in bands]}
    raw.update(changes)
    return raw


def enumerate_(**kw):
    return enumerate_observations(HLS, project='test-project', geometry=GEOMETRY,
        start='2020-08-01', end='2020-09-01', workload_tag='run-source', **kw)


def test_installed_list_images_adapter_paginates_and_preserves_identity(offline_ee, monkeypatch):
    calls = []
    def assets(params):
        calls.append(params)
        if 'pageToken' not in params:
            first = asset('one'); first['id'] = 'one'
            return {'assets':[first], 'nextPageToken':'second'}
        return {'assets':[asset('two')]}  # same acquisition time, distinct source
    monkeypatch.setattr(ee.data, 'listAssets', assets)
    manifest = enumerate_(page_size=1)
    assert manifest['complete'] and manifest['status'] == 'complete'
    assert [s['system_index'] for s in manifest['scenes']] == ['one','two']
    assert all(s['collection'] == HLS and s['modality'] == 'HLS' for s in manifest['scenes'])
    assert manifest['scenes'][0]['asset_id'] == HLS + '/one'
    assert isinstance(manifest['scenes'][0]['time_ms'], int)
    assert manifest['scenes'][0]['native_projections']['B2']['crsCode'] == 'EPSG:32736'
    assert calls[1]['pageToken'] == 'second'
    assert 'workloadTag' not in calls[0] and 'project' not in calls[0]
    assert '2020-09-01T00:00:00Z' in calls[0]['filter']
    assert manifest['telemetry']['workload_tag_applied'] is False
    assert len(manifest['metadata_sha256']) == 64


@pytest.mark.parametrize('response,status', [({'images':[]},'complete_empty'),
    ({'images':[], 'nextPageToken':'repeat'},'incomplete')])
def test_empty_vs_repeated_page_token(offline_ee, monkeypatch, response, status):
    monkeypatch.setattr(ee.data,'listImages',lambda p: response)
    manifest = enumerate_()
    assert manifest['status'] == status
    assert manifest['complete'] == (status == 'complete_empty')


def test_failures_conflicts_and_cap_are_never_complete(offline_ee, monkeypatch):
    monkeypatch.setattr(ee.data,'listImages',Mock(side_effect=RuntimeError('transport failed')))
    assert enumerate_()['status'] == 'query_failed'
    monkeypatch.setattr(ee.data,'listImages',lambda p: {'images':[asset(),asset(properties={'system:index':'different'})]})
    assert enumerate_()['status'] == 'identity_conflict'
    monkeypatch.setattr(ee.data,'listImages',lambda p: {'images':[asset('a'),asset('b')]})
    assert enumerate_(max_scenes=1)['complete'] is False
    assert enumerate_(max_scenes=1)['status'] == 'incomplete'


def test_half_open_window_and_duplicate_identity(offline_ee, monkeypatch):
    monkeypatch.setattr(ee.data,'listImages',lambda p: {'images':[asset(),asset(),
        asset('end',time='2020-09-01T00:00:00Z')]})
    manifest = enumerate_()
    assert manifest['complete']
    assert len(manifest['scenes']) == 1
    assert manifest['excluded_scenes'][0]['exclusion_reason'] == 'outside_half_open_window'


def test_query_plan_budget_and_project_guards(offline_ee, monkeypatch):
    plan = observation_query_plan(HLS,project='test-project',geometry=GEOMETRY,
        start='2020-08-01',end='2020-09-01',workload_tag='run-source')
    request = Mock(); monkeypatch.setattr(ee.data,'listImages',request)
    assert inspect_sources(plan,execute=False)['status'] == 'budget_not_executed'
    request.assert_not_called()
    with pytest.raises(ValueError,match='project'):
        enumerate_observations(HLS,project='wrong',geometry=GEOMETRY,
            start='2020-08-01',end='2020-09-01',workload_tag='x')
    with pytest.raises(ValueError,match='original'):
        observation_query_plan('merged',project='test-project',geometry=GEOMETRY,
            start='2020-08-01',end='2020-09-01',workload_tag='x')


def test_s1_eligibility_preserves_outside_contract_scenes(offline_ee, monkeypatch):
    good = asset(collection=S1, properties={'system:index':'one','instrumentMode':'IW',
        'resolution_meters':10,'transmitterReceiverPolarisation':['VV','VH'],
        'relativeOrbitNumber_start':34,'orbitProperties_pass':'DESCENDING'})
    bad = asset('hh',collection=S1,properties={'system:index':'hh','instrumentMode':'EW',
        'resolution_meters':40,'transmitterReceiverPolarisation':['HH','HV']})
    monkeypatch.setattr(ee.data,'listImages',lambda p: {'images':[good,bad]})
    result = enumerate_observations(S1,project='test-project',geometry=GEOMETRY,
        start='2020-08-01',end='2020-09-01',workload_tag='x')
    assert len(result['scenes']) == len(result['excluded_scenes']) == 1
    assert result['scenes'][0]['relative_orbit'] == 34
    assert result['scenes'][0]['orbit_pass'] == 'DESCENDING'
    assert result['excluded_scenes'][0]['exclusion_reason'] == 'outside_s1_iw10_vvvh_contract'


def test_hls_oracle_retains_cloud_shadow_values_and_unknown_qa():
    values = np.arange(18,dtype=float).reshape(3,6) / 100
    valid = np.ones_like(values,dtype=bool); valid[2,0] = False
    out = hls_observation_values(values,np.array([2,8,0]),valid,np.array([True,True,False]))
    np.testing.assert_array_equal(out[:2,:6],values[:2])
    assert out[:,-1].tolist() == [0,0,-1]
    assert out[:,-2].tolist() == [127,127,62]
    assert np.isnan(out[2,0]) and np.isnan(out[2,6])


def test_s1_oracle_power_common_area_and_whole_target_denominator():
    result = s1_observation_values([-10,-20],[-10,-20],[30,40],
        [True,True],[True,True],[True,False],[.5,.5])
    assert result[0] == pytest.approx(.055)
    assert result[1] == pytest.approx(.055)
    assert result[2] == 30 and result[3] == 7 and result[4] == 1
    partial = s1_observation_values([-10,-20],[-10,-20],[30,40],
        [True,False],[True,True],[False,False],[.5,.5])
    assert partial[0] == pytest.approx(.1) and partial[4] == .5 and partial[3] == 3
    # The second half is outside the scene footprint, represented by absent area.
    edge = s1_observation_values([-10],[-10],[30],[True],[True],[True],[.5])
    assert edge[0] == pytest.approx(.1) and edge[4] == .5
    absent = s1_observation_values([-10],[-10],[30],[False],[True],[True],[1])
    assert np.isnan(absent[:2]).all() and absent[4] == 0 and absent[3] == 4


def test_real_observation_graphs_serialize_without_changing_detection(offline_ee):
    for collection,bands in [(HLS,HLS_OBSERVATION_BANDS),(S1,S1_OBSERVATION_BANDS)]:
        scene = {'collection':collection,'asset_id':collection+'/one',
            'system_index':'one','time_ms':1596240000000,'eligibility':'eligible'}
        if collection == S1:
            scene.update(relative_orbit=34,orbit_pass='DESCENDING')
        serialized = observation_image(scene,grid=GRID).serialize()
        for name in bands: assert name in serialized
        assert 'time_ms' not in bands
        assert 'Image.reproject' in serialized
        if collection == S1:
            assert 'Image.reduceResolution' in serialized and 'Reducer.sum' in serialized
            assert 'Image.exp' in serialized and 'Image.unmask' in serialized
            assert 'ImageCollection.mean' not in serialized
        else:
            assert 'Image.resample' not in serialized  # nearest-neighbour, no bit interpolation
    with pytest.raises(ValueError,match='30'):
        observation_image(scene,grid=dict(GRID,transform=[10,0,0,0,-10,0]))


def test_native_metadata_fallback_retains_version_and_rejects_invalid_grid(offline_ee, monkeypatch):
    listed = asset(); listed.pop('bands')
    full = asset(updateTime='2026-10-15T00:00:00Z',version=42)
    monkeypatch.setattr(ee.data,'listImages',lambda p: {'images':[listed]})
    request = Mock(return_value=full); monkeypatch.setattr(ee.data,'getAsset',request)
    result = enumerate_()
    assert result['complete']
    assert result['scenes'][0]['source_version'] == 42
    request.assert_called_once_with(HLS+'/one')
    full['bands'][0]['grid']['affineTransform']['scaleX'] = 'bad'
    result = enumerate_()
    assert not result['complete'] and result['status'] == 'query_failed'


@pytest.mark.parametrize('collection,use_wkt', [(HLS, True), (S1, False)])
def test_native_grid_accepts_wkt_and_omitted_zero_shears(offline_ee, monkeypatch,
                                                         collection, use_wkt):
    from pyproj import CRS
    raw = asset(collection=collection)
    if collection == S1:
        raw['properties'].update(instrumentMode='IW', resolution_meters=10,
            transmitterReceiverPolarisation=['VV','VH'],
            relativeOrbitNumber_start=34, orbitProperties_pass='DESCENDING')
    for band in raw['bands']:
        grid = band['grid']
        grid['dimensions'] = {'width': 3660, 'height': 3660}
        grid['affineTransform'].pop('shearX')
        grid['affineTransform'].pop('shearY')
        if use_wkt:
            grid['crsWkt'] = CRS.from_epsg(32736).to_wkt()
            grid.pop('crsCode')
    monkeypatch.setattr(ee.data, 'listImages', lambda p: {'images': [raw]})
    result = enumerate_observations(collection, project='test-project', geometry=GEOMETRY,
        start='2020-08-01', end='2020-09-01', workload_tag='run-source')
    assert result['complete'] and result['status'] == 'complete'
    scene = result['scenes'][0]
    grid = scene['native_projections']['VV' if collection == S1 else 'B2']
    assert ('crsWkt' in grid) == use_wkt
    assert 'shearX' not in grid['affineTransform']


@pytest.mark.parametrize('collection',[HLS,'NASA/HLS/HLSS30/v002'])
def test_equatorial_grid_omitted_zero_translation_is_not_missing_projection(offline_ee,monkeypatch,collection):
    # Frozen native-grid shape observed in T49MCV 2025-10: translateY=0 omitted.
    raw=asset('T49MCV',collection=collection)
    for band in raw['bands']:
        band['grid'].update(crsCode='EPSG:32649',dimensions={'width':3660,'height':3660},
            affineTransform={'scaleX':30,'scaleY':-30,'translateX':300000})
    monkeypatch.setattr(ee.data,'listImages',lambda params:{'images':[raw]})
    result=enumerate_observations(collection,project='test-project',
        geometry={'type':'Point','coordinates':[110,-.01]},start='2020-08-01',end='2020-09-01',workload_tag='equator')
    assert result['complete'] and len(result['scenes'])==1
    assert result['scenes'][0]['native_projections']['B2']['affineTransform'].get('translateY',0)==0
    # Missing scales still produce a singular transform and cannot be accepted.
    for band in raw['bands']:band['grid']['affineTransform'].pop('scaleY')
    assert not enumerate_observations(collection,project='test-project',
        geometry={'type':'Point','coordinates':[110,-.01]},start='2020-08-01',end='2020-09-01',workload_tag='equator')['complete']


def test_page_cap_and_s1_all_excluded_remain_distinct_from_zero(offline_ee, monkeypatch):
    monkeypatch.setattr(ee.data,'listImages',lambda p: {'images':[asset()], 'nextPageToken':'more'})
    assert enumerate_(max_pages=1)['failure_reason'] == 'page_cap_exceeded'
    bad = asset('outside',collection=S1,properties={'system:index':'outside','instrumentMode':'EW'})
    monkeypatch.setattr(ee.data,'listImages',lambda p: {'images':[bad]})
    result = enumerate_observations(S1,project='test-project',geometry=GEOMETRY,
        start='2020-08-01',end='2020-09-01',workload_tag='x')
    assert result['status'] == 'complete' and result['complete']
    assert result['scenes'] == [] and len(result['excluded_scenes']) == 1


def test_native_extent_excludes_catalog_false_positive_without_losing_identity(offline_ee, monkeypatch):
    from pyproj import Transformer
    collection = 'NASA/HLS/HLSS30/v002'
    geometry = {'type':'Polygon','coordinates':[[[-110.8,58.9],[-110.5,58.9],
        [-110.5,59.1],[-110.8,59.1],[-110.8,58.9]]]}
    x,y = Transformer.from_crs('EPSG:4326','EPSG:32612',always_xy=True).transform(-110.65,59)
    good,bad = asset('local',collection=collection),asset('far',collection=collection)
    for band in good['bands']:
        band['grid']['crsCode'] = 'EPSG:32612'
        band['grid']['affineTransform'].update(translateX=x-5000,translateY=y+5000)
    for band in bad['bands']:
        band['grid']['crsCode'] = 'EPSG:32601'
        band['grid']['affineTransform'].update(translateX=99960,translateY=-1899960)
        band['grid']['dimensions'] = {'width':3660,'height':3660}
    monkeypatch.setattr(ee.data,'listImages',lambda p: {'images':[good,bad]})
    result = enumerate_observations(collection,project='test-project',geometry=geometry,
        start='2020-08-01',end='2020-09-01',workload_tag='native-extent')
    assert result['complete'] and result['status'] == 'complete'
    assert [s['system_index'] for s in result['scenes']] == ['local']
    assert [(s['system_index'],s['exclusion_reason']) for s in result['excluded_scenes']] == [
        ('far','native_extent_disjoint')]
    assert result['excluded_scenes'][0]['metadata_sha256']


def test_native_extent_requires_dimensions_before_accepting_catalog_hit(offline_ee, monkeypatch):
    raw = asset()
    for band in raw['bands']:
        band['grid'].pop('dimensions')
    monkeypatch.setattr(ee.data,'listImages',lambda p: {'images':[raw]})
    result = enumerate_()
    assert not result['complete'] and result['status'] == 'query_failed'
    assert 'native grid dimensions' in result['failure_reason']


def evaluate_s1_graph(serialized, source, overlaps):
    """Evaluate the real serialized recipe on a tiny native-pixel overlap fixture.

    This is an arithmetic/graph oracle, not an EE reprojection emulator. The
    independently supplied overlaps encode documented reduceResolution weights.
    Unsupported graph operations fail rather than silently approximating them.
    """
    graph = json.loads(serialized)
    cache = {}
    def bands(value):
        return value if isinstance(value,dict) else {'constant':np.ma.array(value)}
    def evaluate(node):
        if 'valueReference' in node:
            ref = node['valueReference']
            if ref not in cache: cache[ref] = evaluate(graph['values'][ref])
            return cache[ref]
        if 'constantValue' in node: return node['constantValue']
        if 'arrayValue' in node: return [evaluate(v) for v in node['arrayValue']['values']]
        call = node['functionInvocationValue']; name = call['functionName']
        a = {k:evaluate(v) for k,v in call['arguments'].items()}
        if name == 'Image.load': return source
        if name == 'Projection': return a
        if name == 'Reducer.sum': return 'sum'
        if name == 'Element.set': return a['object']
        if name == 'Image.constant': return bands(a['value'])
        if name == 'Image.select': return {n:a['input'][n] for n in a['bandSelectors']}
        if name == 'Image.rename': return dict(zip(a['names'],a['input'].values()))
        if name == 'Image.reproject': return a['image']
        if name == 'Image.toFloat': return a['value']
        if name == 'Image.exp': return {n:np.ma.exp(v) for n,v in a['value'].items()}
        if name == 'Image.mask':
            return {n:np.ma.array((~np.ma.getmaskarray(v)).astype(float)) for n,v in a['image'].items()}
        if name == 'Image.unmask':
            assert a['sameFootprint'] is False
            fill = next(iter(a['value'].values()))
            return {n:np.ma.array(np.ma.filled(v,fill)) for n,v in a['input'].items()}
        if name == 'Image.reduceResolution':
            assert a['reducer'] == 'sum' and a['bestEffort'] is False
            return {n:np.ma.array(np.sum(np.ma.filled(v,0)*overlaps)) for n,v in a['image'].items()}
        if name == 'Image.updateMask':
            mask = next(iter(a['mask'].values()))
            return {n:np.ma.array(v,mask=np.ma.getmaskarray(v) | ~np.asarray(mask,dtype=bool)) for n,v in a['image'].items()}
        if name == 'Image.addBands': return dict(a['dstImg'],**a['srcImg'])
        operations = {'Image.and':np.logical_and,'Image.divide':np.ma.divide,
            'Image.multiply':np.ma.multiply,'Image.add':np.ma.add,'Image.gt':np.greater}
        if name in operations:
            left,right = bands(a['image1']),bands(a['image2'])
            assert len(left)==len(right)==1
            return {next(iter(left)):operations[name](next(iter(left.values())),next(iter(right.values())))}
        raise AssertionError('unimplemented graph operation '+name)
    return evaluate({'valueReference':graph['result']})


@pytest.mark.parametrize('vv_valid,vh_valid,area,expected_fraction',[
    ([True,True],[True,True],[.5,.5],1),
    ([True,False],[True,True],[.5,.5],.5),
    ([False,False],[True,True],[.5,.5],0),
    ([True],[True],[.5],.5),
])
def test_actual_serialized_s1_graph_matches_numeric_overlap_oracle(offline_ee,vv_valid,vh_valid,area,expected_fraction):
    size = len(area)
    vv,vh = np.array([-10.,-20.])[:size],np.array([-10.,-20.])[:size]
    angles = np.array([30.,40.])[:size]
    source = {'VV':np.ma.array(vv,mask=~np.array(vv_valid)),
              'VH':np.ma.array(vh,mask=~np.array(vh_valid)), 'angle':np.ma.array(angles)}
    scene = dict(collection=S1,asset_id=S1+'/one',system_index='one',time_ms=1596240000000,
                 relative_orbit=34,orbit_pass='DESCENDING')
    actual = evaluate_s1_graph(observation_image(scene,grid=GRID).serialize(),source,np.array(area))
    expected = s1_observation_values(vv,vh,angles,vv_valid,vh_valid,[True]*size,area)
    for band,value in zip(S1_OBSERVATION_BANDS,expected):
        if np.isnan(value): assert np.ma.is_masked(actual[band])
        else: assert float(actual[band]) == pytest.approx(value)
    assert float(actual['valid_fraction']) == expected_fraction

def test_half_open_acquisition_window_never_filters_asset_end_time(offline_ee,monkeypatch):
    calls=[]
    def assets(params):
        calls.append(params)
        # An acquisition can have missing endTime or an endTime after the query
        # boundary. Membership is solely system:time_start/startTime.
        return {'assets':[asset(time='2020-08-31T23:59:59Z')]}
    monkeypatch.setattr(ee.data,'listAssets',assets)
    result=enumerate_()
    assert result['complete'] and len(result['scenes'])==1
    assert 'endTime' not in calls[0]['filter']
    assert 'startTime < "2020-09-01T00:00:00Z"' in calls[0]['filter']
