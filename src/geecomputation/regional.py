"""Offline single-UTM regional core/query/output planning; no EE imports.

Geometry is WGS84 Polygon/MultiPolygon GeoJSON. Feature windows are an explicit
mapping from each YYYY-MM to {pre: {start,end}, post: {start,end}}. A config_patch
contains only spatial/time fields; the authorized caller supplies project,
budget, output and other Config fields. Batch plans are declarations, not jobs.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re

from .config import utc_datetime


def _sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _iso(value):
    return utc_datetime(value).isoformat().replace('+00:00','Z')


def build_region_plan(*,geometry,grid,months,feature_windows,core_size=1024,halo_pixels=0):
    """Partition fixed pixels, request halo context, and crop output to owned core.

    Shared polygon boundaries have zero area; no positive area has two owners
    within one month. Query halos can overlap and must not be mosaicked directly.
    Cross-zone/antimeridian requests explicitly remain unavailable in this v1.
    """
    from pyproj import Transformer
    from shapely.geometry import box, mapping, shape
    from shapely.ops import transform
    grid=deepcopy(grid)
    affine=grid.get('transform',[]);crs=grid.get('crs','')
    if (not re.fullmatch(r'EPSG:(326|327)(0[1-9]|[1-5][0-9]|60)',crs)
            or len(affine)!=6 or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in affine)
            or affine[0]!=30 or affine[1]!=0 or affine[3]!=0 or affine[4]!=-30):
        raise ValueError('Explicit north-up 30m WGS84 UTM grid required')
    if any(type(grid.get(k)) is not int or grid[k]<1 for k in ('width','height')):
        raise ValueError('Positive integer grid dimensions required')
    if type(core_size) is not int or core_size<1 or type(halo_pixels) is not int or halo_pixels<0:
        raise ValueError('Positive integer core_size and nonnegative integer halo_pixels required')
    if (not isinstance(months,list) or not months or any(not isinstance(m,str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',m) for m in months)
            or len(set(months))!=len(months)):
        raise ValueError('Unique nonempty YYYY-MM months required')
    months=sorted(months)
    if not isinstance(feature_windows,dict) or set(feature_windows)!=set(months):
        raise ValueError('Feature windows must cover exactly the requested months')
    windows={};intervals={}
    for month in months:
        year,number=map(int,month.split('-'))
        try:
            first=datetime(year,number,1,tzinfo=timezone.utc)
            following=datetime(year+(number==12),number%12+1,1,tzinfo=timezone.utc)
            window={period:{key:_iso(feature_windows[month][period][key]) for key in ('start','end')} for period in ('pre','post')}
        except (KeyError,TypeError,ValueError,OverflowError) as exc:
            raise ValueError('Valid month and explicit pre/post feature windows required') from exc
        pre,post=window['pre'],window['post']
        if not utc_datetime(pre['start'])<utc_datetime(pre['end'])<=utc_datetime(post['start'])<utc_datetime(post['end']):
            raise ValueError('Feature windows require positive half-open pre/post intervals')
        windows[month]=window
        intervals[month]={'start':_iso(first),'end':_iso(following)}
    plan=dict(schema_version='region-plan-v1',geometry=deepcopy(geometry),grid=grid,months=months,
        feature_windows=windows,core_size=core_size,halo_pixels=halo_pixels,units=[],status='planned',
        unavailable_reasons=[],limitations=[
            'Offline geometry/ownership plan only; no source enumeration, pixel equivalence or scientific validation.',
            'Only one nominal six-degree UTM zone/hemisphere; cross-zone and antimeridian splitting not implemented.',
            'Geographic polygon edges are represented by their projected vertices.',
            'Halo is algorithm context in pixels, separate from candidate sampling buffers.',
            'Query halos overlap; downstream output must crop to core geometry on the declared core grid.'])
    def finish():
        plan['plan_id']=_sha(plan)
        return plan
    geom=shape(geometry)
    if geom.is_empty:
        plan['status']='empty_geometry'
        return finish()
    if geom.geom_type not in ('Polygon','MultiPolygon') or not geom.is_valid or geom.area<=0:
        raise ValueError('Valid polygonal geometry required')
    west,south,east,north=geom.bounds
    if not all(math.isfinite(v) for v in geom.bounds) or west < -180 or east > 180 or south < -90 or north > 90:
        raise ValueError('Finite WGS84 geographic geometry required')
    zone=int(crs[-2:]);zone_west=-180+6*(zone-1)
    if east-west>180:
        plan['unavailable_reasons'].append('antimeridian')
    elif west<zone_west-1e-9 or east>zone_west+6+1e-9:
        plan['unavailable_reasons'].append('cross_utm')
    if south < -80 or north > 84:
        plan['unavailable_reasons'].append('outside_utm_latitudes')
    if (crs.startswith('EPSG:326') and south < 0) or (crs.startswith('EPSG:327') and north > 0):
        plan['unavailable_reasons'].append('cross_utm_hemisphere')
    if plan['unavailable_reasons']:
        plan['status']='unavailable'
        return finish()
    forward=Transformer.from_crs('EPSG:4326',crs,always_xy=True)
    reverse=Transformer.from_crs(crs,'EPSG:4326',always_xy=True)
    projected=transform(forward.transform,geom)
    x0,y0=affine[2],affine[5]
    extent=box(x0,y0-30*grid['height'],x0+30*grid['width'],y0)
    if not extent.buffer(1e-6).covers(projected):
        raise ValueError('Requested geometry outside declared grid; enlarge the plan grid')
    projected=projected.intersection(extent)
    plan['projected_geometry']=mapping(projected)
    def pixel_grid(column,row,width,height,halo=0):
        return dict(crs=crs,transform=[30,0,x0+30*(column-halo),0,-30,y0-30*(row-halo)],
                    width=width+2*halo,height=height+2*halo)
    def bounds(g):
        a=g['transform']
        return (a[2],a[5]-30*g['height'],a[2]+30*g['width'],a[5])
    def geographic_bbox(g):
        return list(reverse.transform_bounds(*bounds(g),densify_pts=21))
    blocks=[]
    def partition(column,row,width,height):
        g=pixel_grid(column,row,width,height)
        core=projected.intersection(box(*bounds(g)))
        if core.is_empty or core.area<=0:
            return
        bbox=geographic_bbox(g)
        # Split the parent's oversized bbox before invoking Config. Pixel grid
        # origin stays frozen, including uneven edge blocks and recursive cuts.
        if (bbox[2]-bbox[0])*(bbox[3]-bbox[1])>.25 and max(width,height)>1:
            if width>=height and width>1:
                half=width//2
                partition(column,row,half,height);partition(column+half,row,width-half,height)
            else:
                half=height//2
                partition(column,row,width,half);partition(column,row+half,width,height-half)
        else:
            blocks.append((column,row,width,height,g,core))
    step=min(core_size,1024)
    for row in range(0,grid['height'],step):
        for column in range(0,grid['width'],step):
            partition(column,row,min(step,grid['width']-column),min(step,grid['height']-row))
    for month in months:
        window=windows[month]
        for column,row,width,height,core_grid,core in blocks:
            query_grid=pixel_grid(column,row,width,height,halo_pixels)
            bbox=geographic_bbox(query_grid)
            if (bbox[0]<zone_west-1e-9 or bbox[2]>zone_west+6+1e-9
                    or bbox[1]<-80 or bbox[3]>84
                    or (crs.startswith('EPSG:326') and bbox[1]<0)
                    or (crs.startswith('EPSG:327') and bbox[3]>0)):
                plan.update(status='unavailable',units=[],unavailable_reasons=['query_cross_utm'])
                return finish()
            reasons=[]
            if max(query_grid['width'],query_grid['height'])>1024:
                reasons.append('config_grid_dimensions')
            if (bbox[2]-bbox[0])*(bbox[3]-bbox[1])>.25:
                reasons.append('config_bbox_area')
            for period,days in (('pre',93),('post',62)):
                if (utc_datetime(window[period]['end'])-utc_datetime(window[period]['start'])).total_seconds()>days*86400:
                    reasons.append('config_'+period+'_duration')
            owner=_sha(dict(month=month,core_grid=core_grid))
            unit=dict(owner=owner,month=month,month_interval=intervals[month],feature_windows=deepcopy(window),
                crs=crs,core=mapping(core),query=mapping(box(*bounds(query_grid))),output=mapping(core),
                core_grid=core_grid,query_grid=query_grid,crop_to_core=True,halo_pixels=halo_pixels,halo_metres=30*halo_pixels,
                execution='batch_export_plan' if reasons else 'config',config_unavailable_reasons=reasons)
            if not reasons:
                unit['config_patch']=dict(bbox=bbox,grid=query_grid,start=window['post']['start'],end=window['post']['end'],
                    pre_start=window['pre']['start'],pre_end=window['pre']['end'])
            plan['units'].append(unit)
    return finish()
