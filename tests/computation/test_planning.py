from copy import deepcopy
from datetime import datetime,timezone
import pytest

from geecomputation.planning import build_cube_plan, select_support


def scene(index='a',time='2025-12-20T00:00:00Z'):
    return dict(collection='NASA/HLS/HLSL30/v002',system_index=index,
                asset_id='NASA/HLS/HLSL30/v002/'+index,time_ms=int(datetime.fromisoformat(time.replace('Z','+00:00')).timestamp()*1000),
                modality='HLS',eligibility='eligible',properties={},native_projections={},source_version=1,
                source_update_time='2026-01-01',metadata_sha256='raw-response')


def manifest(scenes=None):
    return dict(schema_version='source-manifest-v1',complete=True,pagination_complete=True,status='complete',
                query={'start':'2025-12-01T00:00:00Z','end':'2026-01-01T00:00:00Z'},scenes=[scene()] if scenes is None else scenes)


def grid(size=1024):
    return dict(crs='EPSG:32635',transform=[30,0,500000,0,-30,1000000],width=size,height=size,chip_size=256)


def support(identifier='event',bounds=(500000,969280,530720,1000000),kind='candidate'):
    x0,y0,x1,y1=bounds
    return dict(support_id=identifier,kind=kind,crs='EPSG:32635',geometry={'type':'Polygon','coordinates':[[[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]]},original_draw_id=identifier)


def test_large_float64_product_splits_and_covers_full_support():
    bands=[dict(name='b'+str(i),dtype='float64',unit='declared',recipe='legacy-product-v1') for i in range(21)]
    plan=build_cube_plan(source_manifest=manifest(),supports=[support()],grid=grid(),window={'start':'2025-12-01','end':'2026-01-01'},bands=bands)
    assert len(plan['units'])==16
    assert len({u['unit_id'] for u in plan['units']})==16
    assert all(u['estimated_uncompressed_bytes']==11010048 for u in plan['units'])
    assert sum(u['pixel_grid']['dimensions']['width']*u['pixel_grid']['dimensions']['height'] for u in plan['units'])==1024**2


def test_overlapping_queries_and_outer_dimensions_do_not_change_physical_key():
    a=manifest();b=deepcopy(a);b['query']['geometry']={'type':'Point','coordinates':[27,8]};b['query_finished_at']='later'
    b['scenes'][0]['metadata_sha256']='different-raw-metadata'
    kwargs=dict(grid=grid(),window={'start':'2025-12-01','end':'2026-01-01'},bands=['blue'])
    s1=support('candidate',(500001,999000,501000,999999));s2=support('audit',(500500,999200,501500,999800),'audit')
    plan=build_cube_plan(source_manifest=[a,b],supports=[s1,s2],**kwargs)
    assert len(plan['units'])==1 and plan['units'][0]['support_ids']==['audit','candidate']
    single=build_cube_plan(source_manifest=a,supports=[s1],**dict(kwargs,grid=grid(512)))
    assert single['units'][0]['unit_id']==plan['units'][0]['unit_id']
    changed=deepcopy(a);changed['scenes'][0]['source_version']=2
    assert build_cube_plan(source_manifest=changed,supports=[s1],**kwargs)['units'][0]['unit_id']!=plan['units'][0]['unit_id']
    assert select_support(plan,'audit')['unit_ids']==[plan['units'][0]['unit_id']]


def test_window_cutoff_and_empty_are_distinct_from_failed_query():
    plan=build_cube_plan(source_manifest=manifest([]),supports=[support()],grid=grid(),
                         window={'start':'2025-12-01','end':'2026-02-01'},bands=['blue'])
    assert plan['effective_window']['end']=='2026-01-01T00:00:00Z'
    assert plan['censoring_reason']=='followup_cutoff' and plan['supports'][0]['status']=='no_observation'
    bad=manifest([]);bad['complete']=False;bad['status']='query_failed'
    with pytest.raises(ValueError,match='complete'):
        build_cube_plan(source_manifest=bad,supports=[support()],grid=grid(),window={'start':'2025-12-01','end':'2026-01-01'},bands=['blue'])


def test_temporal_boundary_and_physical_recipe_grid_changes():
    a=manifest([scene('before','2025-11-30T23:59:59Z'),scene('first','2025-12-01T00:00:00Z'),scene('end','2026-01-01T00:00:00Z')])
    kw=dict(source_manifest=a,supports=[support('draw',(500001,999000,501000,999999),'reference_negative')],grid=grid(),window={'start':'2025-12-01','end':'2026-01-01'},bands=['blue'])
    p=build_cube_plan(**kw)
    assert [u['scene']['system_index'] for u in p['units']]==['first']
    moved=grid();moved['transform'][2]-=30
    assert build_cube_plan(**dict(kw,grid=moved))['units'][0]['unit_id']!=p['units'][0]['unit_id']
    assert build_cube_plan(**dict(kw,bands=[dict(name='blue',dtype='float32',unit='raw',recipe='new')]))['units'][0]['unit_id']!=p['units'][0]['unit_id']


def test_internal_bands_count_toward_payload_and_outside_draw_rejected():
    kw=dict(source_manifest=manifest(),supports=[support()],grid=grid(),window={'start':'2025-12-01','end':'2026-01-01'},bands=[dict(name='data',dtype='float64',unit='x',recipe='x'),dict(name='sample_mask',dtype='float64',unit='bool',recipe='x',internal=True)])
    plan=build_cube_plan(**kw,max_request_bytes=1048576)
    assert all(u['estimated_uncompressed_bytes']<=1048576 for u in plan['units'])
    with pytest.raises(ValueError,match='outside'):
        build_cube_plan(**dict(kw,supports=[support('out',(499000,999000,501000,1000000))]))


def test_unenumerated_temporal_or_spatial_support_rejected():
    kw=dict(source_manifest=manifest(),supports=[support()],grid=grid(),window={'start':'2025-11-01','end':'2026-01-01'},bands=['blue'])
    with pytest.raises(ValueError,match='query coverage'):
        build_cube_plan(**kw)
    m=manifest();m['query']['geometry']={'type':'Point','coordinates':[0,0]}
    with pytest.raises(ValueError,match='query coverage'):
        build_cube_plan(**dict(kw,source_manifest=m,window={'start':'2025-12-01','end':'2026-01-01'}))


def test_entire_requested_followup_after_cutoff_is_explicit_empty():
    p=build_cube_plan(source_manifest=manifest([]),supports=[support()],grid=grid(),window={'start':'2026-02-01','end':'2026-03-01'},bands=['blue'])
    assert p['effective_window']=={'start':'2026-01-01T00:00:00Z','end':'2026-01-01T00:00:00Z'}
    assert not p['units'] and p['censoring_reason']=='followup_cutoff'
