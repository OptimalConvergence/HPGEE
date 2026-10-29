"""Original-source enumeration and independent per-scene observation recipes.

No authentication on import. Enumeration requires an already initialized matching
EE project. listImages translates to listAssets and does not support workloadTag;
its elapsed time is request wall time, never an EECU measurement.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import time

import numpy as np
from pyproj import CRS, Transformer
from shapely.geometry import Polygon, shape
from shapely.ops import transform

from hpgee.workflows.config import utc_datetime
from hpgee.compute.features import COMMON6, HLS_COLLECTIONS, HLS_BANDS, clear_fmask

S1_COLLECTION = 'COPERNICUS/S1_GRD'
HLS_OBSERVATION_BANDS = COMMON6 + ('Fmask','source_valid_bits','clear')
S1_OBSERVATION_BANDS = ('VV_power','VH_power','angle','source_valid_bits','valid_fraction')
SCHEMA_VERSION = 'source-manifest-v1'


def _sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')


def observation_query_plan(collection, *, project, geometry, start, end, workload_tag, page_size=500,
                           max_pages=1000, max_scenes=100000):
    """Freeze a bounded metadata query offline; it grants no execution budget."""
    if collection not in (*HLS_COLLECTIONS.values(), S1_COLLECTION):
        raise ValueError('collection must be an original supported collection ID')
    if not isinstance(project,str) or not project.strip():
        raise ValueError('project must be explicit')
    if not isinstance(geometry,dict) or 'type' not in geometry:
        raise ValueError('geometry must be frozen GeoJSON')
    if not workload_tag or not isinstance(workload_tag,str):
        raise ValueError('workload_tag must be explicit')
    if any(type(v) is not int or v < 1 for v in (page_size,max_pages,max_scenes)) or page_size > 1000:
        raise ValueError('positive integer query caps required; page_size <= 1000')
    start,end = utc_datetime(start),utc_datetime(end)
    if start >= end:
        raise ValueError('positive UTC half-open window required')
    plan = dict(collection=collection,project=project,geometry=geometry,
        start=start.isoformat().replace('+00:00','Z'),end=end.isoformat().replace('+00:00','Z'),
        workload_tag=workload_tag,page_size=page_size,max_pages=max_pages,max_scenes=max_scenes)
    # Copy nested geometry, and require serializable, finite query content.
    return json.loads(json.dumps(dict(plan,query_plan_sha256=_sha(plan)),allow_nan=False))


def _manifest(plan):
    return dict(schema_version=SCHEMA_VERSION,collection=plan['collection'],project=plan['project'],
        query=plan,query_started_at=None,query_finished_at=None,status='budget_not_executed',
        complete=False,pagination_complete=False,scenes=[],excluded_scenes=[],pages=0,
        telemetry={'workload_tag':plan['workload_tag'],'workload_tag_applied':False,
            'wall_seconds':0,'reported_eecu_hours':None},
        limitations=['Public asset IDs do not guarantee immutable source pixels.',
            'Catalog start dates do not prove regional archive completeness.',
            'listImages/listAssets metadata requests have no workloadTag parameter.'])


def _identity(raw, collection):
    prefix = 'projects/earthengine-public/assets/'
    candidates = []
    props = raw.get('properties',{})
    for key in ('id','name'):
        value = raw.get(key)
        if value:
            value = value.removeprefix(prefix)
            if value.startswith(collection+'/'):
                value = value[len(collection)+1:]
            elif '/' in value:
                raise ValueError('identity_conflict: asset outside original collection')
            candidates.append(value)
    if props.get('system:index') is not None:
        candidates.append(props['system:index'])
    if not candidates or any(not isinstance(v,str) or not v or '/' in v for v in candidates):
        raise ValueError('identity_conflict: missing or invalid original source identity')
    if len(set(candidates)) != 1:
        raise ValueError('identity_conflict: source names and system:index disagree')
    return candidates[0]


def _scene(raw, collection):
    index = _identity(raw,collection)
    props = raw.get('properties',{})
    timestamp = raw.get('startTime')
    if timestamp is not None:
        time_ms = round(utc_datetime(timestamp).timestamp()*1000)
    else:
        time_ms = props.get('system:time_start')
        if type(time_ms) is not int:
            raise ValueError('source acquisition time missing or not integer milliseconds')
    if 'system:time_start' in props and props['system:time_start'] != time_ms:
        raise ValueError('identity_conflict: conflicting acquisition times')
    projections = {b['id']:b['grid'] for b in raw.get('bands',[]) if 'grid' in b}
    for grid in projections.values():
        affine = grid.get('affineTransform',{})
        # ProtoJSON omits scalar zeroes, including equatorial translations.
        values = [affine.get(k,0)
                  for k in ('scaleX','shearX','translateX','shearY','scaleY','translateY')]
        crs = [grid.get(key) for key in ('crsCode','crsWkt') if grid.get(key)]
        if (len(crs)!=1 or not isinstance(crs[0],str) or
                any(type(v) not in (int,float) or not math.isfinite(v) for v in values)
                or values[0]*values[4]-values[1]*values[3] == 0):
            raise ValueError('invalid native projection metadata')
    modality = 'S1' if collection == S1_COLLECTION else 'HLS'
    result = dict(collection=collection,system_index=index,asset_id=collection+'/'+index,
        time_ms=time_ms,modality=modality,properties=props,bands=raw.get('bands',[]),
        native_projections=projections,metadata_sha256=_sha(raw),
        source_update_time=raw.get('updateTime'),source_version=raw.get('version'),
        eligibility='eligible',band_contract=list(S1_OBSERVATION_BANDS if modality=='S1' else HLS_OBSERVATION_BANDS))
    if modality == 'S1':
        result.update(relative_orbit=props.get('relativeOrbitNumber_start'),orbit_pass=props.get('orbitProperties_pass'))
        if (props.get('instrumentMode') != 'IW' or props.get('resolution_meters') != 10
                or not {'VV','VH'}.issubset(props.get('transmitterReceiverPolarisation',[]))):
            result.update(eligibility='excluded',exclusion_reason='outside_s1_iw10_vvvh_contract')
        elif result['relative_orbit'] is None or result['orbit_pass'] not in ('ASCENDING','DESCENDING'):
            raise ValueError('S1 orbit geometry missing')
    return result


def _native_extent_intersects(projections, query, projected_queries):
    """Verify the actual band grid footprint; catalog region hits can be broad."""
    seen = set()
    for grid in projections.values():
        affine = grid['affineTransform']
        dimensions = grid.get('dimensions', {})
        width,height = dimensions.get('width'),dimensions.get('height')
        if any(type(v) is not int or v < 1 for v in (width,height)):
            raise ValueError('native grid dimensions missing or invalid')
        crs = grid.get('crsCode') or grid.get('crsWkt')
        coefficients = tuple(affine.get(k,0)
                             for k in ('scaleX','shearX','translateX','shearY','scaleY','translateY'))
        key = (crs,width,height,coefficients)
        if key in seen:
            continue
        seen.add(key)
        if crs not in projected_queries:
            transformer = Transformer.from_crs('EPSG:4326',CRS.from_user_input(crs),always_xy=True)
            projected_queries[crs] = transform(transformer.transform,query)
        a,b,c,d,e,f = coefficients
        footprint = Polygon([(a*x+b*y+c,d*x+e*y+f) for x,y in
                             ((0,0),(width,0),(width,height),(0,height))])
        if projected_queries[crs].intersects(footprint):
            return True
    return False


def inspect_sources(plan, *, execute=False):
    """Execute a frozen plan only when the caller explicitly permits execution."""
    checked = observation_query_plan(**{k:v for k,v in plan.items() if k != 'query_plan_sha256'})
    if checked != plan:
        raise ValueError('query plan SHA mismatch')
    if not execute:
        return _manifest(plan)
    return enumerate_observations(**{k:v for k,v in plan.items() if k != 'query_plan_sha256'})


def enumerate_observations(collection, *, project, geometry, start, end, workload_tag, page_size=500,
                           max_pages=1000, max_scenes=100000):
    """Fully paginate original collection metadata; partial results stay incomplete.

    The optional caps bound work, not source completeness. Missing native band
    grids are filled by one bounded getAsset request per listed source, never by
    a collection-wide getInfo. Transport/parser failures remain visible.
    """
    import ee
    plan = observation_query_plan(collection,project=project,geometry=geometry,start=start,end=end,
        workload_tag=workload_tag,page_size=page_size,max_pages=max_pages,max_scenes=max_scenes)
    if ee.data._get_projects_path() != 'projects/'+project:
        raise ValueError('initialized EE project does not match requested project')
    result = _manifest(plan)
    result['query_started_at'] = _now()
    began = time.monotonic()
    params = {'parent':collection,'startTime':plan['start'],
        'filter':f'startTime < "{plan["end"]}"',
        'region':json.dumps(geometry,sort_keys=True),'pageSize':page_size,'view':'FULL'}
    tokens,identities,metadata = set(),{},[]
    lower,upper = (round(utc_datetime(plan[k]).timestamp()*1000) for k in ('start','end'))
    try:
        query_geometry = shape(geometry)
        projected_queries = {}
        # For wide or antimeridian-spanning geometries, projection of sparse
        # GeoJSON edges is not a safe exclusion proof; keep catalog hits.
        minx,miny,maxx,maxy = query_geometry.bounds
        check_native_extent = maxx-minx <= 5 and maxy-miny <= 5
        while True:
            response = ee.data.listImages(params)
            result['pages'] += 1
            if not isinstance(response,dict) or not isinstance(response.get('images'),list):
                raise ValueError('invalid listImages response shape')
            for raw in response['images']:
                index = _identity(raw,collection)
                needed = ('VV','VH','angle') if collection==S1_COLLECTION else HLS_BANDS[next(k for k,v in HLS_COLLECTIONS.items() if v==collection)]+('Fmask',)
                if not set(needed).issubset({b.get('id') for b in raw.get('bands',[]) if b.get('grid')}):
                    full = ee.data.getAsset(collection+'/'+index)
                    if _identity(full,collection) != index:
                        raise ValueError('identity_conflict: projection metadata source mismatch')
                    raw = dict(full,**raw)
                    raw['bands'] = full.get('bands',[])
                    if not set(needed).issubset({b.get('id') for b in raw['bands'] if b.get('grid')}):
                        # Outside-contract S1 may legitimately lack VV/VH.
                        if collection != S1_COLLECTION or _scene(raw,collection)['eligibility']=='eligible':
                            raise ValueError('native projection metadata missing')
                scene = _scene(raw,collection)
                if index in identities:
                    if identities[index] != scene['metadata_sha256']:
                        raise ValueError('identity_conflict: duplicate source has different metadata')
                    continue
                if len(identities) >= max_scenes:
                    result.update(status='incomplete',failure_reason='scene_cap_exceeded')
                    break
                identities[index] = scene['metadata_sha256']; metadata.append(raw)
                if not lower <= scene['time_ms'] < upper:
                    scene.update(eligibility='excluded',exclusion_reason='outside_half_open_window')
                elif scene['eligibility']=='eligible' and check_native_extent and not _native_extent_intersects(
                        scene['native_projections'],query_geometry,projected_queries):
                    scene.update(eligibility='excluded',exclusion_reason='native_extent_disjoint')
                result['scenes' if scene['eligibility']=='eligible' else 'excluded_scenes'].append(scene)
            if result['status']=='incomplete': break
            token = response.get('nextPageToken')
            if not token:
                result.update(complete=True,pagination_complete=True,
                    status='complete' if identities else 'complete_empty')
                break
            if token in tokens or result['pages'] >= max_pages:
                result.update(status='incomplete',failure_reason='repeated_page_token' if token in tokens else 'page_cap_exceeded')
                break
            tokens.add(token); params['pageToken'] = token
    except Exception as exc:
        result.update(status='identity_conflict' if 'identity_conflict' in str(exc) else 'query_failed',
            failure_reason=str(exc),complete=False)
    result['scenes'].sort(key=lambda s:(s['time_ms'],s['collection'],s['system_index']))
    result['excluded_scenes'].sort(key=lambda s:(s['time_ms'],s['collection'],s['system_index']))
    result.update(query_finished_at=_now(),metadata_sha256=_sha(metadata))
    result['telemetry']['wall_seconds'] = time.monotonic()-began
    return result


def hls_observation_values(values, fmask, valid, fmask_valid):
    """Local acceptance oracle, preserving per-band validity and unknown Fmask."""
    values,valid = np.asarray(values,float),np.asarray(valid,bool)
    qa,qa_valid = np.asarray(fmask),np.asarray(fmask_valid,bool)
    bits = (valid.astype(np.uint8)*(1 << np.arange(6))).sum(axis=-1) + qa_valid.astype(np.uint8)*64
    clear = np.where(qa_valid,clear_fmask(qa).astype(np.int8),-1)
    return np.concatenate([np.where(valid,values,np.nan),np.where(qa_valid,qa,np.nan)[...,None],
        bits[...,None],clear[...,None]],axis=-1)


def s1_observation_values(vv_db,vh_db,angle,vv_valid,vh_valid,angle_valid,area_fractions):
    """Local area-overlap oracle for one target pixel, including absent footprint.

    area_fractions are native intersections divided by WHOLE target pixel area;
    they need not sum to one when the source footprint ends inside the target.
    """
    vv,vh,angle = np.asarray(vv_db,float),np.asarray(vh_db,float),np.asarray(angle,float)
    weights = np.asarray(area_fractions,float)
    if np.any(weights<0) or not np.isfinite(weights).all() or weights.sum()>1+1e-9:
        raise ValueError('area fractions must partition a target pixel')
    common = np.asarray(vv_valid,bool)&np.asarray(vh_valid,bool)
    av = np.asarray(angle_valid,bool)
    fraction = weights[common].sum(); angle_fraction = weights[av].sum()
    powers = [np.sum(10**(v[common]/10)*weights[common])/fraction if fraction else np.nan for v in (vv,vh)]
    a = np.sum(angle[av]*weights[av])/angle_fraction if angle_fraction else np.nan
    return np.array([*powers,a,(3 if fraction else 0)+(4 if angle_fraction else 0),fraction])


def observation_image(scene, *, grid):
    """Build a lazy single-scene image; scene acquisition UTC ms stays in index.

    HLS uses default nearest-neighbour for values and categorical masks. S1
    reduceResolution sum uses overlap / full target area (EE's documented
    weights); power numerator divided by valid_fraction gives the common-valid
    area mean. Unmask(sameFootprint=False) makes absent footprint contribute 0.
    See https://developers.google.com/earth-engine/guides/resample .
    """
    import ee
    transform = grid.get('transform',[])
    if (len(transform)!=6 or not all(math.isfinite(v) for v in transform)
            or transform[0]!=30 or transform[4]!=-30 or transform[1]!=0 or transform[3]!=0
            or not grid.get('crs') or not CRS.from_user_input(grid['crs']).utm_zone):
        raise ValueError('frozen UTM 30m affine grid required')
    collection = scene['collection']
    if collection not in (*HLS_COLLECTIONS.values(),S1_COLLECTION):
        raise ValueError('unsupported original collection')
    if scene.get('eligibility','eligible') != 'eligible':
        raise ValueError('scene is outside observation contract')
    if scene['asset_id'] != collection+'/'+scene['system_index']:
        raise ValueError('scene identity mismatch')
    source = ee.Image(scene['asset_id'])
    def target(image):
        return image.reproject(crs=grid['crs'],crsTransform=transform)
    if collection != S1_COLLECTION:
        sensor = next(k for k,v in HLS_COLLECTIONS.items() if v==collection)
        reflectance = source.select(list(HLS_BANDS[sensor]),list(COMMON6))
        qa = source.select('Fmask')
        bits = reflectance.mask().unmask(0,False).multiply(ee.Image.constant([1,2,4,8,16,32]).rename(list(COMMON6))).reduce(ee.Reducer.sum())
        qa_valid = qa.mask().unmask(0,False)
        bits = bits.add(qa_valid.multiply(64)).rename('source_valid_bits')
        clear = qa.bitwiseAnd(30).eq(0).And(qa.rightShift(6).bitwiseAnd(3).neq(3))
        clear = clear.unmask(-1,False).rename('clear')
        result = target(reflectance.addBands(qa).addBands(bits).addBands(clear)).select(list(HLS_OBSERVATION_BANDS))
    else:
        vv,vh,angle = source.select('VV'),source.select('VH'),source.select('angle')
        common = vv.mask().And(vh.mask()).unmask(0,False)
        def overlap_sum(image):
            return target(image.reduceResolution(reducer=ee.Reducer.sum(),maxPixels=1024,bestEffort=False))
        fraction = overlap_sum(common).rename('valid_fraction')
        powers = []
        for band,name in ((vv,'VV_power'),(vh,'VH_power')):
            # Base image is native band, preserving its projection through pow.
            power = band.divide(10).multiply(math.log(10)).exp().updateMask(common).unmask(0,False)
            powers.append(overlap_sum(power).divide(fraction).updateMask(fraction.gt(0)).rename(name))
        angle_fraction = overlap_sum(angle.mask().unmask(0,False))
        angle_mean = overlap_sum(angle.unmask(0,False)).divide(angle_fraction).updateMask(angle_fraction.gt(0)).rename('angle')
        bits = fraction.gt(0).multiply(3).add(angle_fraction.gt(0).multiply(4)).rename('source_valid_bits')
        result = powers[0].addBands(powers[1]).addBands(angle_mean).addBands(bits).addBands(fraction)
    return result.toFloat().set({'source_collection':collection,'source_index':scene['system_index'],
        'system:time_start':scene['time_ms'],'observation_recipe':'hls-raw-nearest-v1' if collection!=S1_COLLECTION else 's1-common-area-power-v1',
        's1_support_limitation':'valid bits describe aggregate outputs, not native subpixels' if collection==S1_COLLECTION else '',
        'relative_orbit':scene.get('relative_orbit'),'orbit_pass':scene.get('orbit_pass')})
