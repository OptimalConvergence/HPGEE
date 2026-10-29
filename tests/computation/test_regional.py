"""Real local geometry checks; no EE initialization or service requests."""

import pytest
from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform, unary_union
import hpgee.planning.regional as regional
from hpgee.workflows.config import Config


def grid(width=8,height=4):
    return dict(crs='EPSG:32635',transform=[30,0,500000,0,-30,1000000],width=width,height=height)


def geometry(g):
    a=g['transform'];projected=box(a[2],a[5]-30*g['height'],a[2]+30*g['width'],a[5])
    return mapping(transform(Transformer.from_crs(g['crs'],'EPSG:4326',always_xy=True).transform,projected))


def windows(months=('2020-08',)):
    return {m:dict(pre={'start':m+'-01','end':m+'-02'},post={'start':m+'-02','end':m+'-03'}) for m in months}


def build(g=None,**changes):
    g=g or grid()
    kw=dict(geometry=geometry(g),grid=g,months=['2020-08'],feature_windows=windows(),core_size=4,halo_pixels=1)
    kw.update(changes)
    return regional.build_region_plan(**kw)


def test_core_union_has_no_positive_overlap_and_output_crops_to_original_support():
    p=build();rows=p['units'];cores=[shape(u['core']) for u in rows]
    assert p['status']=='planned' and len(rows)==2
    assert sum(c.area for c in cores)==pytest.approx(unary_union(cores).area)
    assert unary_union(cores).symmetric_difference(shape(p['projected_geometry'])).area<1e-4
    assert all(shape(u['output']).equals(shape(u['core'])) for u in rows)
    assert len({u['owner'] for u in rows})==2
    assert all(u['crop_to_core'] is True for u in rows)


def test_query_halo_is_full_context_and_not_clipped_to_requested_region():
    row=build()['units'][0]
    assert shape(row['query']).covers(shape(row['core']))
    assert row['query_grid']['width']==6 and row['query_grid']['height']==6
    assert row['query_grid']['transform']==[30,0,499970,0,-30,1000030]
    assert row['halo_pixels']==1 and row['halo_metres']==30
    assert shape(row['query']).bounds==(499970,999850,500150,1000030)


def test_months_are_unique_half_open_and_owner_is_stable():
    months=['2020-09','2020-08']
    p=build(months=months,feature_windows=windows(months))
    assert len(p['units'])==4
    august=[u for u in p['units'] if u['month']=='2020-08']
    assert august[0]['month_interval']=={'start':'2020-08-01T00:00:00Z','end':'2020-09-01T00:00:00Z'}
    assert [u['owner'] for u in august]==[u['owner'] for u in build()['units']]
    assert p['plan_id']==build(months=list(reversed(months)),feature_windows=windows(months))['plan_id']
    with pytest.raises(ValueError,match='month'):
        build(months=['2020-08','2020-08'])


def test_empty_geometry_is_explicit_and_does_not_fabricate_units():
    p=build(geometry={'type':'Polygon','coordinates':[]})
    assert p['status']=='empty_geometry' and p['units']==[]


def test_large_grid_is_partitioned_without_relaxing_config_limits():
    g=grid(2050,1025)
    with pytest.raises(ValueError,match='small ROI|dimensions'):
        Config.from_dict(dict(project='test',bbox=[27,8,28,9],start='2020-08-01',end='2020-09-01',
            pre_start='2020-07-01',pre_end='2020-08-01',grid=g,output='x',budget_eecu_hours=1,seed=0))
    p=build(g,core_size=1024,halo_pixels=0)
    assert len(p['units'])>=6
    assert all(u['core_grid']['width']<=1024 and u['core_grid']['height']<=1024 for u in p['units'])
    assert all(u['execution']=='config' for u in p['units'])
    assert sum(shape(u['core']).area for u in p['units'])==pytest.approx(2050*1025*900,rel=1e-7)


def test_large_query_halo_is_explicit_batch_plan():
    row=build(grid(1024,1024),core_size=1024,halo_pixels=1)['units'][0]
    assert row['query_grid']['width']>1024 and row['execution']=='batch_export_plan'
    assert 'config_grid_dimensions' in row['config_unavailable_reasons']


@pytest.mark.parametrize('geom,reason',[
    (mapping(box(29.9,8,30.1,8.1)),'cross_utm'),
    ({'type':'Polygon','coordinates':[[[179,1],[-179,1],[-179,2],[179,2],[179,1]]]},'antimeridian'),
])
def test_unsupported_zone_requests_are_recorded_unavailable(geom,reason):
    p=build(geometry=geom)
    assert p['status']=='unavailable' and p['units']==[]
    assert reason in p['unavailable_reasons']


def test_geometry_outside_declared_grid_never_silently_clips():
    g=grid();outside=geometry(grid(9,4))
    with pytest.raises(ValueError,match='outside'):
        build(g,geometry=outside)


@pytest.mark.parametrize('changes',[
    {'core_size':0},{'halo_pixels':-1},{'months':['2020-13']},
    {'feature_windows':{}},{'grid':dict(grid(),transform=[60,0,500000,0,-30,1000000])},
])
def test_invalid_plan_contracts_rejected(changes):
    with pytest.raises(ValueError):
        build(**changes)


def test_config_patches_pass_unchanged_small_roi_config():
    p=build(grid(2050,1025),core_size=1024,halo_pixels=0)
    for row in p['units']:
        cfg=Config.from_dict(dict(project='offline-plan',output='unsubmitted',budget_eecu_hours=1,seed=0,
                                  **row['config_patch']))
        assert cfg.grid==row['query_grid']


def test_high_latitude_bbox_is_subdivided_before_config():
    g=grid(1024,1024)
    g['transform'][5]=Transformer.from_crs('EPSG:4326',g['crs'],always_xy=True).transform(27,75)[1]
    p=build(g,core_size=1024,halo_pixels=0)
    assert len(p['units'])>1 and all(row['execution']=='config' for row in p['units'])
    assert sum(shape(row['core']).area for row in p['units'])==pytest.approx(1024*1024*900,rel=1e-7)


def test_small_positive_polygon_is_not_lost_by_area_threshold():
    g=grid()
    tiny=box(500000.01,999999.98,500000.0101,999999.9801)
    geom=mapping(transform(Transformer.from_crs(g['crs'],'EPSG:4326',always_xy=True).transform,tiny))
    p=build(g,geometry=geom)
    assert p['status']=='planned' and len(p['units'])==1
    assert shape(p['units'][0]['core']).area>0


def test_halo_query_crossing_zone_is_explicitly_unavailable():
    g=grid(100,100)
    x,y=Transformer.from_crs('EPSG:4326',g['crs'],always_xy=True).transform(29.99,9)
    g['transform']=[30,0,x,0,-30,y]
    p=build(g,geometry=mapping(box(29.995,8.99,29.996,8.991)),core_size=100,halo_pixels=50)
    assert p['status']=='unavailable' and p['units']==[]
    assert 'query_cross_utm' in p['unavailable_reasons']
