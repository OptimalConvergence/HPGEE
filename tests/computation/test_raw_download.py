from datetime import date
from geecomputation.raw_download import image_date, window90, chunks, pack_limit

def test_dates_and_closed_days():
    assert image_date('B17RNK20180715_30m') == date(2018,7,15)
    assert image_date('804503220180608') == date(2018,6,8)
    assert image_date('missing') is None
    assert window90(date(2018,6,8), date(2018,7,10)) == ('2018-05-11','2018-08-09')

def test_edges_cover_exact_native_grid():
    grid={'crsCode':'EPSG:32610','affineTransform':dict(scaleX=30,shearX=0,translateX=499980,shearY=0,scaleY=-30,translateY=4500000),'dimensions':{'width':3660,'height':3660}}
    blocks=chunks(grid,1024)
    assert len(blocks)==16
    assert sum(b['dimensions']['width']*b['dimensions']['height'] for b in blocks)==3660**2
    assert blocks[-1]['dimensions']=={'width':588,'height':588}
    assert blocks[-1]['affineTransform']['translateX']==499980+3072*30

def test_payload_includes_all_qa_and_times():
    assert pack_limit(1024,1024,8,4)==1
    assert pack_limit(512,512,8,4)==4

def test_native_s1_crop_stays_on_source_grid():
    from geecomputation.raw_download import native_window
    from shapely.geometry import box,mapping
    grid={'crsCode':'EPSG:32610','affineTransform':dict(scaleX=10,shearX=0,translateX=0,shearY=0,scaleY=-10,translateY=100),'dimensions':{'width':100,'height':100}}
    # Same-CRS geometry avoids projection ambiguity in this exact edge fixture.
    result=native_window(grid,mapping(box(13,42,67,89)),geometry_crs='EPSG:32610')
    assert result['dimensions']=={'width':6,'height':5}
    assert result['affineTransform']['translateX']==10
    assert result['affineTransform']['translateY']==90

def test_raw_s1_30_has_no_angle_validity_bit():
    import ee
    import numpy as np
    from ee import apitestcase
    from .test_observations import evaluate_s1_graph
    from geecomputation.raw_download import raw_image
    helper=apitestcase.ApiTestCase();helper.setUp()
    try:
        collection='COPERNICUS/S1_GRD'
        scene=dict(collection=collection,asset_id=collection+'/one',system_index='one',time_ms=1596240000000)
        grid={'crsCode':'EPSG:32610','affineTransform':dict(scaleX=30,shearX=0,translateX=499980,shearY=0,scaleY=-30,translateY=4500000),'dimensions':{'width':1,'height':1}}
        source={name:np.ma.array([v]) for name,v in [('VV',-10.),('VH',-20.),('angle',35.)]}
        actual=evaluate_s1_graph(raw_image(scene,grid,'s1-30').serialize(),source,np.array([1.]))
        assert float(actual['source_valid_bits'])==3
    finally:helper.tearDown();ee.Reset()

def test_hls_native_wkt_grid_is_preserved_and_matches_same_tile_epsg():
    from geecomputation.raw_download import grid_spec,same_hls_grid
    from pyproj import CRS
    grid={'crsWkt':CRS.from_epsg(32610).to_wkt(),'affineTransform':dict(scaleX=30,scaleY=-30,translateX=499980,translateY=4500000),'dimensions':{'width':3660,'height':3660}}
    other={k:v for k,v in grid.items() if k!='crsWkt'};other['crsCode']='EPSG:32610'
    assert grid_spec(grid)['crs']==grid['crsWkt']
    assert same_hls_grid(grid,other)
    shifted={**other,'affineTransform':{**other['affineTransform'],'translateX':500010}}
    assert not same_hls_grid(grid,shifted)
