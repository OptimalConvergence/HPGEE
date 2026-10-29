"""Offline fixed-core candidate masks/components and planar spatial relations.

Components are engineering identities, never wildfire events or burn dates.
Query halo must be explicitly cropped to its owned core before indexing.
"""
import base64
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np


def _sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _grid(grid):
    result=deepcopy(grid);a=result.get('transform',[])
    if (not re.fullmatch(r'EPSG:(326|327)(0[1-9]|[1-5][0-9]|60)',result.get('crs',''))
            or len(a)!=6 or any(type(v) not in (int,float) or not math.isfinite(v) for v in a)
            or a[0]!=30 or a[1]!=0 or a[3]!=0 or a[4]!=-30
            or any(type(result.get(k)) is not int or result[k]<1 for k in ('width','height'))):
        raise ValueError('Explicit fixed north-up UTM 30m grid required; geographic/antimeridian indexing unavailable')
    result['transform']=[float(v) if v else 0.0 for v in a]
    return {k:result[k] for k in ('crs','transform','width','height')}


def _unknown():
    return dict(event_id=None,last_unburned=None,first_burned=None,burn_interval_status='unknown',reburn_relation=None)


def _packed(candidate,valid):
    pack=lambda array:base64.b64encode(np.packbits(array.ravel(),bitorder='little').tobytes()).decode('ascii')
    result=dict(encoding='numpy-packbits-little-row-major-base64-v1',shape=list(candidate.shape),
                candidate_bits=pack(candidate),valid_bits=pack(valid))
    return dict(result,sha256=_sha(result))


def decode_masks(index):
    """Recover exact owned-core masks, including unknown pixels, without EE."""
    packed=index['mask'];payload={k:v for k,v in packed.items() if k!='sha256'}
    if _sha(payload)!=packed.get('sha256'):raise ValueError('candidate/valid mask SHA mismatch')
    if packed['encoding']!='numpy-packbits-little-row-major-base64-v1':raise ValueError('unsupported mask encoding')
    shape=tuple(packed['shape']);count=math.prod(shape)
    if len(shape)!=2 or any(type(v) is not int or v<1 for v in shape):raise ValueError('invalid mask shape')
    def unpack(key):
        raw=base64.b64decode(packed[key],validate=True)
        if len(raw)!=(count+7)//8:raise ValueError('mask byte length differs from shape')
        return np.unpackbits(np.frombuffer(raw,dtype='uint8'),bitorder='little')[:count].reshape(shape).astype(bool)
    candidate,valid=unpack('candidate_bits'),unpack('valid_bits')
    if np.any(candidate&~valid):raise ValueError('unknown pixel cannot be an accepted candidate')
    return candidate,valid


def index_candidates(array,valid,grid,model_id,month,calibration_status,*,core_window=None):
    """Index one candidate core. ``core_window`` is (row,column,height,width).

    Valid input values must be binary 0/1. Invalid cells remain unknown and are
    encoded independently; their fill values never become observed unburned.
    Component pixel spans are local [row,start_column,stop_column), connectivity8.
    """
    from affine import Affine
    from rasterio.features import shapes,rasterize
    grid=_grid(grid);values=np.asarray(array);valid=np.asarray(valid)
    if values.shape!=(grid['height'],grid['width']) or valid.shape!=values.shape or valid.dtype!=np.dtype(bool):
        raise ValueError('candidate array and explicit boolean valid mask must match grid dimensions')
    if np.ma.isMaskedArray(array):valid=valid&~np.ma.getmaskarray(array)
    if not isinstance(model_id,str) or not model_id.strip() or not isinstance(calibration_status,str) or not calibration_status.strip():
        raise ValueError('explicit model_id and calibration_status required')
    if not isinstance(month,str) or not re.fullmatch(r'20(18|19|2[0-5])-(0[1-9]|1[0-2])',month):
        raise ValueError('candidate_month must be YYYY-MM in 2018–2025')
    if core_window is not None:
        if len(core_window)!=4 or any(type(x) is not int for x in core_window):raise ValueError('integer core_window required')
        row,col,height,width=core_window
        if min(row,col)<0 or min(height,width)<1 or row+height>grid['height'] or col+width>grid['width']:
            raise ValueError('core_window outside query grid')
        values=values[row:row+height,col:col+width];valid=valid[row:row+height,col:col+width]
        grid=dict(grid,transform=[30,0,grid['transform'][2]+30*col,0,-30,grid['transform'][5]-30*row],width=width,height=height)
    grid=_grid(grid)
    from pyproj import Transformer
    a=grid['transform']
    geographic=Transformer.from_crs(grid['crs'],'EPSG:4326',always_xy=True).transform_bounds(
        a[2],a[5]-30*grid['height'],a[2]+30*grid['width'],a[5],densify_pts=21)
    if not all(math.isfinite(v) for v in geographic) or geographic[0]>geographic[2]:
        raise ValueError('antimeridian or nonfinite core extent is unavailable; split upstream')
    if np.any(valid&(~np.isfinite(values)|~np.isin(values,(0,1)))):
        raise ValueError('valid candidate values must be finite binary 0/1')
    candidate=valid&(values==1);pixels=int(candidate.sum());observed=int(valid.sum())
    components=[]
    if pixels:
        geometries=[geometry for geometry,value in shapes(candidate.astype('uint8'),mask=candidate,
                    connectivity=8,transform=Affine.identity()) if value==1]
        labels=rasterize([(geometry,number) for number,geometry in enumerate(geometries,1)],
            out_shape=candidate.shape,transform=Affine.identity(),fill=0,dtype='int32')
        if not np.array_equal(labels>0,candidate):raise ValueError('component rasterization changed primary candidate mask')
        spans={label:[] for label in range(1,len(geometries)+1)}
        for row,values_row in enumerate(labels):
            boundaries=np.flatnonzero(np.r_[True,values_row[1:]!=values_row[:-1],True])
            for start,stop in zip(boundaries[:-1],boundaries[1:]):
                label=int(values_row[start])
                if label:spans[label].append([int(row),int(start),int(stop)])
        for runs in sorted(spans.values(),key=lambda rows:rows[0]):
            count=sum(stop-start for row,start,stop in runs)
            identity=dict(model_id=model_id,candidate_month=month,grid=grid,pixel_spans=runs)
            components.append(dict(component_id=_sha(identity),candidate_month=month,model_id=model_id,
                candidate_pixels=count,candidate_area_m2=count*900,pixel_spans=runs,**_unknown()))
    result=dict(schema_version='candidate-core-index-v1',model_id=model_id,candidate_month=month,
        calibration_status=calibration_status,grid=grid,connectivity=8,
        status='no_observation' if not observed else 'empty_candidate' if not pixels else 'candidates_present',
        candidate_pixels=pixels,candidate_area_m2=pixels*900,observed_pixels=observed,
        unknown_pixels=valid.size-observed,observed_noncandidate_pixels=observed-pixels,
        mask=_packed(candidate,valid),components=components,confirmed_new_burn_area_m2=None,
        **_unknown(),limitations=['Candidate month is packaging/inference context, not an evidenced burning month.',
            'Area is 30m grid candidate support, not independently validated burned area.',
            'Components are per owned core; spatial relations do not create events or global component unions.'])
    result['index_id']=_sha(result)
    return result


def index_saved_candidates(path,*,model_id,month,calibration_status,core_window=None):
    """Read the named candidate band and saved mask; never change product bands."""
    import rasterio
    path=Path(path).resolve()
    with rasterio.open(path) as ds:
        bands=[i for i,name in enumerate(ds.descriptions,1) if name=='candidate']
        if len(bands)!=1 or ds.crs is None:raise ValueError('exactly one named candidate band and explicit CRS required')
        data=ds.read(bands[0],masked=True)
        grid=dict(crs=ds.crs.to_string(),transform=list(ds.transform)[:6],width=ds.width,height=ds.height)
        result=index_candidates(data,~np.ma.getmaskarray(data),grid,model_id,month,calibration_status,core_window=core_window)
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    result['mask_reference']=dict(path=str(path),sha256=digest.hexdigest(),band='candidate',core_window=core_window)
    result.pop('index_id');result['index_id']=_sha(result)
    return result


def _row_spans(index,origin):
    grid=index['grid'];a=grid['transform'];ref=origin['transform']
    dx=round((a[2]-ref[2])/30);dy=round((ref[5]-a[5])/30)
    rows={}
    for component in index['components']:
        for row,start,stop in component['pixel_spans']:
            rows.setdefault(row+dy,[]).append((start+dx,stop+dx,component['component_id']))
    return {row:sorted(spans) for row,spans in rows.items()}


def _touches(left,right):
    """Sweep ordered row spans; no dense global pixel cube or component unions."""
    relations={}
    for row,a_spans in left.items():
        for offset in (-1,0,1):
            b_spans=right.get(row+offset,[]);begin=0
            for a,b,aid in a_spans:
                while begin<len(b_spans) and b_spans[begin][1]<a:begin+=1
                cursor=begin
                while cursor<len(b_spans) and b_spans[cursor][0]<=b:
                    c,d,bid=b_spans[cursor]
                    if d>=a:
                        key=(aid,bid);overlap=max(0,min(b,d)-max(a,c)) if offset==0 else 0
                        relations[key]=relations.get(key,0)+overlap
                    cursor+=1
    return relations


def relate_components(indices):
    """Record same-lattice 8-touch and cross-month overlap/adjacency only."""
    unique={}
    for index in indices:
        if index.get('index_id')!=_sha({k:v for k,v in index.items() if k!='index_id'}):
            raise ValueError('candidate index SHA mismatch')
        _grid(index['grid']);decode_masks(index);unique[index['index_id']]=index
    items=sorted(unique.values(),key=lambda row:row['index_id']);reasons=[]
    if items:
        reference=items[0]['grid'];a=reference['transform']
        if any(index['grid']['crs']!=reference['crs'] or any(not math.isclose(delta/30,round(delta/30),rel_tol=0,abs_tol=1e-8)
                for delta in (index['grid']['transform'][2]-a[2],index['grid']['transform'][5]-a[5])) for index in items):
            reasons.append('different_crs_or_lattice')
        if len({index['model_id'] for index in items})>1:reasons.append('different_models')
    result=dict(schema_version='candidate-spatial-relations-v1',status='unavailable' if reasons else 'complete',
        unavailable_reasons=reasons,unique_indices=len(items),relations=[],candidate_area_m2_by_month={},
        confirmed_new_burn_area_m2=None,**_unknown())
    if reasons:return result
    def bounds(index):
        g=index['grid'];x=g['transform'][2];y=g['transform'][5]
        return x,y-30*g['height'],x+30*g['width'],y
    spans={index['index_id']:_row_spans(index,items[0]['grid']) for index in items}
    for i,left in enumerate(items):
        month=left['candidate_month']
        result['candidate_area_m2_by_month'][month]=result['candidate_area_m2_by_month'].get(month,0)+left['candidate_area_m2']
        for right in items[i+1:]:
            same_month=month==right['candidate_month'];a,b,c,d=bounds(left);e,f,g,h=bounds(right)
            if same_month and min(c,g)>max(a,e) and min(d,h)>max(b,f):
                raise ValueError('overlapping core ownership in the same month; crop halo or select one version')
            for (aid,bid),overlap in sorted(_touches(spans[left['index_id']],spans[right['index_id']]).items()):
                result['relations'].append(dict(left_component_id=aid,right_component_id=bid,
                    left_month=month,right_month=right['candidate_month'],connectivity=8,overlap_pixels=overlap,
                    relation_kind='same_month_touch' if same_month else 'cross_month_spatial_overlap' if overlap else 'cross_month_spatial_adjacency',
                    **_unknown()))
    return result
