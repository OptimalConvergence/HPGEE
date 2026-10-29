from pathlib import Path
import numpy as np
import rasterio
from rasterio.transform import Affine
from hpgee.transfer.raw_download import make_unit
from hpgee.transfer.raw_delivery import publish, verified

def test_valid_zero_integer_qa_and_corruption(tmp_path):
    grid={'crsCode':'EPSG:32610','affineTransform':dict(scaleX=30,shearX=0,translateX=499980,shearY=0,scaleY=-30,translateY=4500000),'dimensions':{'width':2,'height':2}}
    scene={'asset_id':'hls/example','time_ms':0}
    unit=make_unit([scene],grid,'hls')
    values=np.ones((8,2,2),dtype='float32');values[0,0,0]=0
    values[6]=2;values[7]=127;values[7,1,1]=0
    path=tmp_path/'raw.tif'
    with rasterio.open(path,'w',driver='GTiff',width=2,height=2,count=8,dtype='float32',crs=grid['crsCode'],transform=Affine(30,0,499980,0,-30,4500000)) as ds:ds.write(values)
    rows=publish(unit,path,tmp_path/'saved')
    assert verified(unit,tmp_path/'saved')
    with rasterio.open(rows[0]['data_path']) as ds:
        data=ds.read(masked=True)
        assert data[0,0,0]==0 and not data.mask[0,0,0]
        assert data.mask[:,1,1].all()
    Path(rows[0]['data_path']).write_bytes(b'corrupt')
    assert not verified(unit,tmp_path/'saved')

def test_partial_pack_not_used_for_benchmark_and_resume_splits():
    from hpgee.transfer.raw_delivery import atoms, pending_units
    grid={'crsCode':'EPSG:32610','affineTransform':dict(scaleX=30,shearX=0,translateX=0,shearY=0,scaleY=-30,translateY=30000),'dimensions':{'width':1024,'height':1024}}
    unit=make_unit([{'asset_id':'hls/a','time_ms':0}],grid,'hls')
    pieces=atoms(unit);completed={pieces[0]['unit_id']}
    assert pending_units([unit],completed,fresh_only=True)==[]
    remaining=pending_units([unit],completed,fresh_only=False)
    assert {u['unit_id'] for u in remaining}=={u['unit_id'] for u in pieces[1:]}

def test_hls_ee_geotiff_normalizes_unknown_utm_wkt_to_epsg(tmp_path):
    # Actual APPLE HLS metadata WKT has an unnamed WGS84 datum, while EE's
    # GeoTIFF normalizes the otherwise identical tile to EPSG:32610.
    wkt=(Path(__file__).parent/'fixtures'/'hls_utm10_unknown.wkt').read_text()
    grid={'crsWkt':wkt,'affineTransform':dict(scaleX=30,shearX=0,translateX=499980,shearY=0,scaleY=-30,translateY=4500000),'dimensions':{'width':2,'height':2}}
    unit=make_unit([{'asset_id':'hls/apple','time_ms':0}],grid,'hls')
    values=np.ones((8,2,2),dtype='float32');values[6]=2;values[7]=127
    path=tmp_path/'ee.tif'
    with rasterio.open(path,'w',driver='GTiff',width=2,height=2,count=8,dtype='float32',crs='EPSG:32610',transform=Affine(30,0,499980,0,-30,4500000)) as ds:ds.write(values)
    rows=publish(unit,path,tmp_path/'saved')
    assert len(rows)==1 and verified(unit,tmp_path/'saved')

def test_s1_30_uses_hls_target_grid_with_normalized_utm_crs(tmp_path):
    wkt=(Path(__file__).parent/'fixtures'/'hls_utm10_unknown.wkt').read_text()
    grid={'crsWkt':wkt,'affineTransform':dict(scaleX=30,shearX=0,translateX=499980,shearY=0,scaleY=-30,translateY=4500000),'dimensions':{'width':2,'height':2}}
    unit=make_unit([{'asset_id':'s1/apple','time_ms':0}],grid,'s1-30')
    values=np.ones((4,2,2),dtype='float32');values[2]=3
    path=tmp_path/'ee.tif'
    with rasterio.open(path,'w',driver='GTiff',width=2,height=2,count=4,dtype='float32',crs='EPSG:32610',transform=Affine(30,0,499980,0,-30,4500000)) as ds:ds.write(values)
    rows=publish(unit,path,tmp_path/'saved')
    assert len(rows)==1 and verified(unit,tmp_path/'saved')
