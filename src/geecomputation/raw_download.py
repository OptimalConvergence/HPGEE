"""Raw HLS/S1 delivery contract, independent of RF and observation packages.

Historical HLS/S1 profiles remain Float32. The explicit ``hls-int16`` raw
profile carries scaled HLS reflectance and separate lossless QA on transport.
Validity bits preserve valid zeroes in both profiles.
"""
from datetime import datetime, timedelta
import copy
import hashlib
import json
import math
import re
from .observations import observation_image, S1_COLLECTION

HLS_BANDS = ('blue','green','red','nir','swir1','swir2','Fmask','source_valid_bits')
HLS_INT16_RECIPE = 'raw-hls-int16-scale1e4-v1'
HLS_INT16_SCALE = 0.0001
HLS_INT16_OFFSET = 0.0
HLS_INT16_OVERFLOW_FLAG_BIT = 7
S1_BANDS = ('VV_power','VH_power','source_valid_bits','valid_fraction')
RECIPE = 'raw-full-support-v1'
S1_30_RECIPE = 'raw-s1-30-value-support-v2'


def image_date(value):
    for token in reversed(re.findall(r'((?:19|20)\d{6})(?!\d)', str(value))):
        try:
            return datetime.strptime(token,'%Y%m%d').date()
        except ValueError:
            continue
    return None


def window90(pre, post):
    gap = (post-pre).days
    if not 0 < gap < 90:
        raise ValueError('Require 0 < post-pre < 90 days')
    remainder = 90 - (gap+1)
    start = pre-timedelta(days=remainder//2)
    return start.isoformat(), (start+timedelta(days=90)).isoformat()


def chunks(grid, size):
    if type(size) is not int or size < 1:
        raise ValueError('positive integer chunk size required')
    width,height = (grid['dimensions'][k] for k in ('width','height'))
    if min(width,height)<1:
        raise ValueError('positive dimensions required')
    result=[]
    for y in range(0,height,size):
        for x in range(0,width,size):
            block=copy.deepcopy(grid); a=block['affineTransform']; src=grid['affineTransform']
            a['translateX']=src.get('translateX',0)+x*src['scaleX']+y*src.get('shearX',0)
            a['translateY']=src.get('translateY',0)+x*src.get('shearY',0)+y*src['scaleY']
            for k in ('shearX','shearY'): a.setdefault(k,0)
            block['dimensions']={'width':min(size,width-x),'height':min(size,height-y)}
            result.append(block)
    return result


def pack_limit(width,height,band_count,requested,byte_limit=40*1024**2,itemsize=4):
    # Actual masked Float32 HLS1144 requests were charged pixels*bands*5,
    # not just the four value bytes. Keep the logical/HTTP/storage budgets
    # distinct and reserve this observed per-band mask overhead at the API
    # boundary. For Int16 this reuses the same one-byte-per-element planning
    # margin; it is not a measured Int16 wire-size claim. The 40MiB target
    # still describes value planes only.
    if type(itemsize) is not int or itemsize not in (2,4):
        raise ValueError('payload itemsize must be 2-byte Int16 or 4-byte Float32')
    pixels=width*height*band_count
    result=min(requested,byte_limit//(pixels*itemsize),(48*1024**2)//(pixels*(itemsize+1)))
    if result<1: raise ValueError('One scene exceeds uncompressed payload budget')
    return result


def grid_spec(pixel):
    a=pixel['affineTransform']
    return {'crs':pixel.get('crsCode') or pixel['crsWkt'],'transform':[a.get(k,0) for k in ('scaleX','shearX','translateX','shearY','scaleY','translateY')]}


def raw_image(scene, pixel, profile):
    """Lazy native HLS / native S1 power / common-valid area S1 30m."""
    if profile in ('hls','hls-int16'):
        if scene['collection']==S1_COLLECTION: raise ValueError('HLS source required')
        source=observation_image(scene,grid=grid_spec(pixel)).select(list(HLS_BANDS))
        if profile=='hls':return source
        import ee
        reflectance=source.select(list(HLS_BANDS[:6])).multiply(10000).round()
        overflow=(reflectance.lt(-32768).Or(reflectance.gt(32767))
                  .reduce(ee.Reducer.max()).unmask(0,False).multiply(1<<HLS_INT16_OVERFLOW_FLAG_BIT))
        fmask=source.select('Fmask').toInt16()
        bits=source.select('source_valid_bits').add(overflow).toInt16()
        return reflectance.toInt16().addBands(fmask).addBands(bits).set({
            'observation_recipe':HLS_INT16_RECIPE,'encoding_scale':HLS_INT16_SCALE,
            'encoding_offset':HLS_INT16_OFFSET,'overflow_flag_bit':HLS_INT16_OVERFLOW_FLAG_BIT})
    import ee
    if scene['collection']!=S1_COLLECTION: raise ValueError('S1 source required')
    if profile=='s1-30':
        return _s1_30_image(scene,grid_spec(pixel))
    if profile!='s1-10': raise ValueError('Unknown raw profile')
    source=ee.Image(scene['asset_id']); vv=source.select('VV'); vh=source.select('VH')
    common=vv.mask().And(vh.mask()).unmask(0,False)
    powers=[b.divide(10).multiply(math.log(10)).exp().rename(n)
            for b,n in ((vv,'VV_power'),(vh,'VH_power'))]
    return powers[0].addBands(powers[1]).addBands(vv.mask().unmask(0,False).add(vh.mask().unmask(0,False).multiply(2)).rename('source_valid_bits')).addBands(common.rename('valid_fraction')).toFloat()


def _s1_30_image(scene, grid):
    """Raw-only common-valid area power; observation/RF defaults stay unchanged."""
    import ee
    from pyproj import CRS
    transform=grid.get('transform',[])
    if (len(transform)!=6 or not all(math.isfinite(v) for v in transform)
            or transform[0]!=30 or transform[4]!=-30 or transform[1]!=0 or transform[3]!=0
            or not grid.get('crs') or not CRS.from_user_input(grid['crs']).utm_zone):
        raise ValueError('frozen UTM 30m affine grid required')
    if scene.get('eligibility','eligible')!='eligible':
        raise ValueError('scene is outside observation contract')
    if scene['asset_id']!=scene['collection']+'/'+scene['system_index']:
        raise ValueError('scene identity mismatch')
    source=ee.Image(scene['asset_id']); vv=source.select('VV'); vh=source.select('VH')
    masks=vv.mask().And(vh.mask())
    # Mask-only images are not reliable support values through EE reduction.
    # Carry 0/1 in a native value band before unmasking outside the footprint.
    common=vv.multiply(0).add(1).updateMask(masks).unmask(0,False)
    def overlap_sum(image):
        return image.reduceResolution(reducer=ee.Reducer.sum(),maxPixels=1024,bestEffort=False).reproject(
            crs=grid['crs'],crsTransform=transform)
    fraction=overlap_sum(common).rename('valid_fraction')
    powers=[]
    for band,name in ((vv,'VV_power'),(vh,'VH_power')):
        power=band.divide(10).multiply(math.log(10)).exp().updateMask(masks).unmask(0,False)
        powers.append(overlap_sum(power).divide(fraction).updateMask(fraction.gt(0)).rename(name))
    bits=fraction.gt(0).multiply(3).rename('source_valid_bits')
    return powers[0].addBands(powers[1]).addBands(bits).addBands(fraction).toFloat().set({
        'source_collection':scene['collection'],'source_index':scene['system_index'],
        'system:time_start':scene['time_ms'],'observation_recipe':S1_30_RECIPE,
        's1_support_limitation':'valid bits describe aggregate outputs, not native subpixels',
        'relative_orbit':scene.get('relative_orbit'),'orbit_pass':scene.get('orbit_pass')})


def make_unit(scenes, pixel, profile):
    if not scenes: raise ValueError('At least one scene required')
    if profile not in ('hls','hls-int16','s1-10','s1-30'):
        raise ValueError('Unknown raw profile')
    is_hls=profile in ('hls','hls-int16')
    bands=HLS_BANDS if is_hls else S1_BANDS
    itemsize=2 if profile=='hls-int16' else 4
    pack_limit(pixel['dimensions']['width'],pixel['dimensions']['height'],len(bands),len(scenes),itemsize=itemsize)
    if len(scenes)>pack_limit(pixel['dimensions']['width'],pixel['dimensions']['height'],len(bands),len(scenes),itemsize=itemsize):
        raise ValueError('Temporal pack exceeds payload budget')
    identity={'sources':[s['asset_id'] for s in scenes],'grid':pixel,'profile':profile,
              'recipe':HLS_INT16_RECIPE if profile=='hls-int16' else S1_30_RECIPE if profile=='s1-30' else RECIPE}
    if profile=='hls-int16':
        identity['encoding']={'dtype':'int16','scale':HLS_INT16_SCALE,'offset':HLS_INT16_OFFSET,
                              'qa_bands':['Fmask','source_valid_bits'],
                              'overflow_flag_bit':HLS_INT16_OVERFLOW_FLAG_BIT}
    uid=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:32]
    return dict(identity,unit_id=uid,scenes=scenes,bands=[f't{i}_{b}' for i in range(len(scenes)) for b in bands],
                logical_bytes=pixel['dimensions']['width']*pixel['dimensions']['height']*len(bands)*itemsize*len(scenes))


def unit_image(unit):
    if unit['profile']=='s1-30' and unit.get('recipe')!=S1_30_RECIPE:
        raise ValueError('S1 30m recipe mismatch; rebuild units with the current recipe')
    if unit['profile']=='hls-int16':
        expected={'dtype':'int16','scale':HLS_INT16_SCALE,'offset':HLS_INT16_OFFSET,
                  'qa_bands':['Fmask','source_valid_bits'],
                  'overflow_flag_bit':HLS_INT16_OVERFLOW_FLAG_BIT}
        if unit.get('recipe')!=HLS_INT16_RECIPE or unit.get('encoding')!=expected:
            raise ValueError('HLS Int16 recipe or scale/QA identity mismatch')
    result=None
    bands=HLS_BANDS if unit['profile'] in ('hls','hls-int16') else S1_BANDS
    for i,scene in enumerate(unit['scenes']):
        image=raw_image(scene,unit['grid'],unit['profile']).rename([f't{i}_{b}' for b in bands])
        result=image if result is None else result.addBands(image)
    return result


def native_window(grid, geometry, geometry_crs='EPSG:4326'):
    """Integer native-pixel envelope of tile intersection, without resampling."""
    from affine import Affine
    from shapely.geometry import shape, box
    from shapely.ops import transform
    from pyproj import Transformer
    a=Affine(*grid_spec(grid)['transform'])
    geom=transform(Transformer.from_crs(geometry_crs,grid_spec(grid)['crs'],always_xy=True).transform,shape(geometry))
    inv=~a
    pixel=transform(lambda x,y: inv*(x,y),geom)
    bounds=pixel.intersection(box(0,0,grid['dimensions']['width'],grid['dimensions']['height'])).bounds
    if not bounds: return None
    x0,y0=map(math.floor,bounds[:2]); x1,y1=map(math.ceil,bounds[2:])
    if x1<=x0 or y1<=y0:return None
    result=copy.deepcopy(grid);tx,ty=a*(x0,y0)
    result['affineTransform'].update(translateX=tx,translateY=ty)
    result['dimensions']={'width':x1-x0,'height':y1-y0}
    return result


def same_hls_grid(left,right):
    """Compare common HLS UTM grids without replacing the recorded native CRS.

    HLS specifies WGS84 UTM. Historical assets sometimes label the WGS84
    ellipsoid with an unnamed datum; require identical UTM zone/hemisphere,
    ellipsoid, metric units, affine and dimensions, not merely a WKT label.
    This predicate is only for HLS common-grid assets, never arbitrary CRSs.
    """
    from pyproj import CRS
    a,b=grid_spec(left),grid_spec(right)
    if left['dimensions']!=right['dimensions'] or a['transform']!=b['transform']:return False
    ca,cb=CRS.from_user_input(a['crs']),CRS.from_user_input(b['crs'])
    if ca.equals(cb):return True
    return bool(ca.utm_zone and ca.utm_zone==cb.utm_zone
        and all(axis.unit_conversion_factor==1 for c in (ca,cb) for axis in c.axis_info)
        and all(abs(c.ellipsoid.semi_major_metre-6378137)<1e-6
                and abs(c.ellipsoid.inverse_flattening-298.257223563)<1e-9 for c in (ca,cb)))
