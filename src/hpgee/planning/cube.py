"""Offline fixed-grid observation plans; no EE authentication or requests.

A unit is a full, globally addressed chip relative to the frozen regional
origin. Supports reference it; query ROI/month/event names never identify it.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

import numpy as np
from hpgee.workflows.config import utc_datetime

MAX_REQUEST_BYTES = 33554432
FOLLOWUP_CUTOFF = '2026-01-01T00:00:00Z'


def _sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _iso(value):
    return utc_datetime(value).isoformat().replace('+00:00','Z')


def projected_support(support, crs):
    """Full original support, transformed locally without replacing its draw."""
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform
    geometry=shape(support['geometry'])
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError('Support geometry must be nonempty and valid')
    source=support.get('crs','EPSG:4326')
    return transform(Transformer.from_crs(source,crs,always_xy=True).transform,geometry) if source!=crs else geometry


def _window(window, cutoff):
    requested={k:_iso(window[k]) for k in ('start','end')}
    a,b=(utc_datetime(requested[k]) for k in ('start','end'))
    if a>=b:
        raise ValueError('Window must have positive UTC half-open duration')
    c=utc_datetime(cutoff)
    return requested,dict(start=_iso(min(a,c)),end=_iso(min(b,c))), 'followup_cutoff' if b>c else None


def source_fingerprint(scene):
    """Hash source interpretation, excluding query provenance/lexical names."""
    identity={k:scene.get(k) for k in ('collection','system_index','asset_id','time_ms','modality',
        'source_update_time','source_version','native_projections','properties','relative_orbit','orbit_pass')}
    # Band metadata affects interpretation even if an upstream publisher does
    # not expose a revision ID. Raw id/name/updateTime wrappers do not.
    identity['bands']=scene.get('bands',[])
    return _sha(identity)


def _check_query_coverage(manifests,support,start,end,crs):
    from shapely.ops import unary_union
    if start>=end:
        return
    groups={}
    for manifest in manifests:
        collection=manifest.get('collection') or next((s['collection'] for s in manifest['scenes']),None)
        groups.setdefault(collection,[]).append(manifest['query'])
    geometry=projected_support(support,crs)
    for queries in groups.values():
        intervals=[(utc_datetime(q['start']).timestamp()*1000,utc_datetime(q['end']).timestamp()*1000,q) for q in queries]
        boundaries=sorted({start,end,*[max(start,min(end,v)) for a,b,q in intervals for v in (a,b)]})
        for a,b in zip(boundaries,boundaries[1:]):
            active=[q for left,right,q in intervals if left<=a and right>=b]
            if not active:
                raise ValueError('Source query coverage does not include requested temporal support')
            if all(q.get('geometry') for q in active):
                footprint=unary_union([projected_support(dict(geometry=q['geometry'],crs='EPSG:4326'),crs) for q in active])
                if not footprint.buffer(1e-4).covers(geometry):
                    raise ValueError('Source query coverage does not include full original spatial support')


def _bands(bands):
    names=[b if isinstance(b,str) else b['name'] for b in bands]
    recipe='s1-common-area-power-v1' if any(name in ('VV_power','VH_power','valid_fraction') for name in names) else 'hls-raw-nearest-v1'
    result=[]
    for b in bands:
        row=dict(name=b,dtype='float32',unit='source-native',recipe=recipe) if isinstance(b,str) else dict(b)
        if not row.get('name') or not row.get('unit') or not row.get('recipe'):
            raise ValueError('Each band requires name, dtype, unit and recipe')
        dtype=np.dtype(row['dtype'])
        if dtype.kind not in 'fiu' or dtype.itemsize>8:
            raise ValueError('Unsupported raster dtype')
        row['dtype']=dtype.name
        result.append(row)
    if not result or len({b['name'] for b in result})!=len(result):
        raise ValueError('Nonempty unique band list required, including internal bands')
    return result


def build_cube_plan(*,source_manifest,supports,grid,window,bands,max_request_bytes=MAX_REQUEST_BYTES):
    """Plan source/chip units and full support references without online work.

    source_manifest may be one complete SourceManifest or several overlapping
    query manifests. All supports retain their original geometry and provenance.
    A support window can narrow the shared temporal union. Full chips may extend
    past the outer ROI; this preserves physical identity at region boundaries.
    """
    from shapely.geometry import box
    manifests=source_manifest if isinstance(source_manifest,list) else [source_manifest]
    if not manifests or any(not m.get('complete') or not m.get('pagination_complete') for m in manifests):
        raise ValueError('Only complete paginated source manifests may form a cube plan')
    if type(max_request_bytes) is not int or not 0<max_request_bytes<=MAX_REQUEST_BYTES:
        raise ValueError('Request budget must be positive and <=32 MiB')
    grid=deepcopy(grid);affine=grid['transform'];crs=grid['crs']
    if (not re.fullmatch(r'EPSG:(326|327)(0[1-9]|[1-5][0-9]|60)',crs)
            or len(affine)!=6 or any(not math.isfinite(v) for v in affine)
            or affine[0]!=30 or affine[4]!=-30 or affine[1]!=0 or affine[3]!=0):
        raise ValueError('Explicit UTM north-up 30m grid origin required')
    for key in ('width','height'):
        if type(grid[key]) is not int or grid[key]<1:
            raise ValueError('Positive grid dimensions required')
    chip=grid.get('chip_size',256)
    if type(chip) is not int or chip<1:
        raise ValueError('Positive fixed chip size required')
    band_contract=_bands(bands)
    bytes_per_pixel=sum(np.dtype(b['dtype']).itemsize for b in band_contract)
    # Halve along the original fixed chip lattice, never derive origins from
    # event bounding boxes. A changed chip size creates a different unit key.
    while chip*chip*bytes_per_pixel>max_request_bytes and chip>1:
        chip=max(1,chip//2)
    if bytes_per_pixel>max_request_bytes:
        raise ValueError('Even one pixel exceeds request byte budget')
    grid['chip_size']=chip
    requested,effective,censoring=_window(window,window.get('followup_cutoff',FOLLOWUP_CUTOFF))
    cutoff=window.get('followup_cutoff',FOLLOWUP_CUTOFF)
    lower,upper=(utc_datetime(effective[k]).timestamp()*1000 for k in ('start','end'))
    source={}
    for m in manifests:
        for scene in m['scenes']:
            if scene.get('eligibility','eligible')!='eligible':
                continue
            if scene.get('asset_id')!=scene['collection']+'/'+scene['system_index'] or type(scene.get('time_ms')) is not int:
                raise ValueError('Exact source identity/time required')
            if lower<=scene['time_ms']<upper:
                source[source_fingerprint(scene)]=deepcopy(scene)
    x0,y0=affine[2],affine[5]
    region=box(x0,y0-grid['height']*30,x0+grid['width']*30,y0)
    saved_supports=[];units={};support_ids=set()
    for original in supports:
        support=deepcopy(original);sid=support['support_id']
        if not isinstance(sid,str) or not sid or sid in support_ids:
            raise ValueError('Unique explicit support_id required')
        support_ids.add(sid)
        shape=projected_support(support,crs)
        if not region.buffer(1e-6).covers(shape):
            raise ValueError('Original support extends outside declared grid; enlarge region instead of replacing draw')
        a,b=shape.bounds[0],shape.bounds[2]
        bottom,top=shape.bounds[1],shape.bounds[3]
        c0,c1=math.floor((a-x0)/(30*chip)),math.floor((b-x0-1e-8)/(30*chip))
        r0,r1=math.floor((y0-top)/(30*chip)),math.floor((y0-bottom-1e-8)/(30*chip))
        c1=max(c0,c1);r1=max(r0,r1)
        sr,se,sc=_window(support.get('window',window),cutoff)
        sl,su=(utc_datetime(se[k]).timestamp()*1000 for k in ('start','end'))
        if sl<lower or su>upper:
            raise ValueError('Support window outside declared temporal union')
        _check_query_coverage(manifests,support,sl,su,crs)
        support.update(requested_window=sr,effective_window=se,followup_cutoff=_iso(cutoff),censoring_reason=sc,unit_ids=[])
        for fingerprint,scene in sorted(source.items()):
            if not sl<=scene['time_ms']<su:
                continue
            for r in range(r0,r1+1):
                for c in range(c0,c1+1):
                    left,top=x0+c*chip*30,y0-r*chip*30
                    footprint=box(left,top-chip*30,left+chip*30,top)
                    intersection=shape.intersection(footprint)
                    if intersection.is_empty or (shape.area>0 and intersection.area<=0):
                        continue
                    chip_affine=[30,0,left,0,-30,top]
                    physical=dict(source_fingerprint=fingerprint,crs=crs,transform=chip_affine,
                                  width=chip,height=chip,bands=band_contract)
                    uid=_sha(physical)
                    if uid not in units:
                        units[uid]=dict(unit_id=uid,scene=scene,source_fingerprint=fingerprint,
                            grid=dict(crs=crs,transform=chip_affine),chip=dict(column=c,row=r,size=chip),
                            pixel_grid=dict(crsCode=crs,affineTransform=dict(zip(('scaleX','shearX','translateX','shearY','scaleY','translateY'),chip_affine)),
                                dimensions=dict(width=chip,height=chip)),bands=band_contract,
                            estimated_uncompressed_bytes=chip*chip*bytes_per_pixel,support_ids=[])
                    units[uid]['support_ids'].append(sid);support['unit_ids'].append(uid)
        support['status']='planned' if support['unit_ids'] else 'no_observation'
        saved_supports.append(support)
    for unit in units.values():
        unit['support_ids']=sorted(set(unit['support_ids']))
    plan=dict(schema_version='cube-plan-v1',grid=grid,requested_window=requested,effective_window=effective,
        followup_cutoff=_iso(cutoff),censoring_reason=censoring,max_request_bytes=max_request_bytes,
        source_manifest_sha256=[_sha(m) for m in manifests],supports=saved_supports,
        units=sorted(units.values(),key=lambda u:u['unit_id']),estimated_unique_bytes=sum(u['estimated_uncompressed_bytes'] for u in units.values()),
        limitations=['Cross-UTM overlaps remain separate physical supports.',
                    'Full chips preserve context beyond logical support; masks remain in support index.',
                    'Public source identity/revision evidence is not a guarantee of immutable provider pixels.'])
    plan['plan_id']=_sha(plan)
    return plan


def select_support(plan,support_id):
    """Offline downstream view: reference blocks without copying event cubes."""
    support=next(s for s in plan['supports'] if s['support_id']==support_id)
    return dict(deepcopy(support),units=[deepcopy(u) for u in plan['units'] if u['unit_id'] in support['unit_ids']],plan_id=plan['plan_id'])
