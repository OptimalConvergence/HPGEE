"""Candidate components are planar grid objects, never accepted wildfire events."""
from copy import deepcopy
import importlib

import numpy as np
import pytest


def api():
    return importlib.import_module('hpgee.planning.candidates')


def grid(left=500000,top=1000000,width=3,height=3):
    return dict(crs='EPSG:32635',transform=[30,0,left,0,-30,top],width=width,height=height)


def index(values,*,g=None,valid=None,month='2020-08',model='frozen-model',**kw):
    values=np.array(values,dtype='float64');g=g or grid(width=values.shape[1],height=values.shape[0])
    return api().index_candidates(values,np.ones(values.shape,dtype=bool) if valid is None else valid,
        g,model,month,'uncalibrated_rf_candidate',**kw)


def test_eight_connected_components_have_exact_masks_and_no_event_inference():
    result=index([[1,0,0],[0,1,0],[0,0,0]])
    assert result['status']=='candidates_present' and len(result['components'])==1
    component=result['components'][0]
    assert component['candidate_pixels']==2 and component['candidate_area_m2']==1800
    assert component['pixel_spans']==[[0,0,1],[1,1,2]]
    assert component['event_id'] is None and component['reburn_relation'] is None
    assert component['last_unburned'] is None and component['first_burned'] is None
    assert component['burn_interval_status']=='unknown'
    assert component['candidate_month']=='2020-08'
    assert result['confirmed_new_burn_area_m2'] is None
    candidate,valid=api().decode_masks(result)
    np.testing.assert_array_equal(candidate,[[1,0,0],[0,1,0],[0,0,0]])
    assert valid.all()


def test_disconnected_components_get_separate_stable_identities():
    a=index([[1,0,0],[0,0,0],[0,0,1]])
    assert len(a['components'])==2
    assert a==index([[1,0,0],[0,0,0],[0,0,1]])
    original=a['components'][0]['component_id']
    for other in (index([[1,0,0],[0,0,0],[0,0,1]],month='2020-09'),
                  index([[1,0,0],[0,0,0],[0,0,1]],model='other-model'),
                  index([[1,0,0],[0,0,0],[0,0,1]],g=grid(left=500030)),
                  index([[1,1,0],[0,0,0],[0,0,1]])):
        assert other['components'][0]['component_id']!=original


def test_unknown_observations_are_not_empty_unburned_masks():
    empty=index([[0,0],[0,0]])
    unknown=index([[0,1],[np.nan,1]],valid=np.zeros((2,2),bool))
    assert empty['status']=='empty_candidate' and unknown['status']=='no_observation'
    assert empty['observed_pixels']==4 and unknown['observed_pixels']==0
    assert unknown['unknown_pixels']==4 and unknown['candidate_pixels']==0
    assert unknown['components']==[]


def test_halo_is_cropped_before_identity_component_area_and_neighbor_relations():
    values=np.ones((4,4))
    cropped=index(values,g=grid(left=499970,top=1000030,width=4,height=4),core_window=(1,1,2,2))
    plain=index([[1,1],[1,1]],g=grid(width=2,height=2))
    assert cropped['grid']==plain['grid'] and cropped['candidate_pixels']==4
    assert cropped['components']==plain['components']
    right=index([[1,1],[1,1]],g=grid(left=500060,width=2,height=2))
    related=api().relate_components([cropped,right])
    assert related['candidate_area_m2_by_month']=={'2020-08':7200}
    assert len(related['relations'])==1
    with pytest.raises(ValueError,match='overlapping core'):
        api().relate_components([plain,index([[1,1],[1,1]],g=grid(left=500030,width=2,height=2))])


@pytest.mark.parametrize('right_values,right_grid',[
    ([[1,0],[0,0]],grid(left=500060,top=999940,width=2,height=2)),
    ([[0,0],[1,0]],grid(left=500060,width=2,height=2)),
])
def test_cross_core_edge_and_diagonal_touch_are_recorded_as_eight_neighbors(right_values,right_grid):
    left=index([[0,0],[0,1]],g=grid(width=2,height=2))
    right=index(right_values,g=right_grid)
    result=api().relate_components([left,right])
    assert len(result['relations'])==1
    relation=result['relations'][0]
    assert relation['relation_kind']=='same_month_touch' and relation['connectivity']==8
    assert relation['event_id'] is None and relation['reburn_relation'] is None


def test_separated_core_pixels_are_not_falsely_linked():
    a=index([[1,0],[0,0]],g=grid(width=2,height=2))
    b=index([[1,0],[0,0]],g=grid(left=500060,width=2,height=2))
    assert api().relate_components([a,b])['relations']==[]


def test_cross_month_persistent_candidate_overlap_is_not_new_burn_or_reburn():
    a=index([[1,0],[0,0]],g=grid(width=2,height=2))
    b=index([[1,0],[0,0]],g=grid(width=2,height=2),month='2020-09')
    result=api().relate_components([a,b])
    assert result['candidate_area_m2_by_month']=={'2020-08':900,'2020-09':900}
    assert result['confirmed_new_burn_area_m2'] is None
    assert result['relations'][0]['relation_kind']=='cross_month_spatial_overlap'
    assert result['relations'][0]['overlap_pixels']==1
    assert result['relations'][0]['event_id'] is None
    assert result['relations'][0]['burn_interval_status']=='unknown'


@pytest.mark.parametrize('change',[
    {'crs':'EPSG:32636'}, {'transform':[30,0,500015,0,-30,1000000]},
])
def test_different_crs_or_lattice_is_explicitly_unavailable(change):
    a=index([[1,0],[0,0]],g=grid(width=2,height=2))
    other=grid(left=500060,width=2,height=2);other.update(change)
    b=index([[1,0],[0,0]],g=other)
    result=api().relate_components([a,b])
    assert result['status']=='unavailable' and result['relations']==[]
    assert 'different_crs_or_lattice' in result['unavailable_reasons']


def test_repeated_identical_index_does_not_duplicate_candidate_area():
    a=index([[1,0],[0,0]],g=grid(width=2,height=2))
    result=api().relate_components([a,deepcopy(a)])
    assert result['candidate_area_m2_by_month']=={'2020-08':900}
    assert result['unique_indices']==1


def test_saved_tiff_preserves_nodata_instead_of_calling_it_unburned(tmp_path):
    import rasterio
    from rasterio.transform import from_origin
    path=tmp_path/'candidate.tif'
    with rasterio.open(path,'w',driver='GTiff',width=2,height=2,count=1,dtype='float32',
        crs='EPSG:32635',transform=from_origin(500000,1000000,30,30),nodata=np.nan) as dst:
        dst.write(np.array([[1,np.nan],[0,1]],dtype='float32'),1);dst.set_band_description(1,'candidate')
    result=api().index_saved_candidates(path,model_id='frozen-model',month='2020-08',calibration_status='uncalibrated')
    assert result['observed_pixels']==3 and result['unknown_pixels']==1
    assert result['candidate_pixels']==2 and result['mask_reference']['band']=='candidate'
    assert result['mask_reference']['path']==str(path.resolve())


def test_nonbinary_valid_candidate_values_and_geographic_grid_are_rejected():
    with pytest.raises(ValueError,match='binary'):
        index([[.3,0],[0,1]])
    g=grid(width=2,height=2);g['crs']='EPSG:4326'
    with pytest.raises(ValueError,match='UTM'):
        index([[1,0],[0,0]],g=g)


def test_cross_month_spatial_adjacency_never_becomes_reburn():
    a=index([[0,1],[0,0]],g=grid(width=2,height=2))
    b=index([[1,0],[0,0]],g=grid(left=500060,width=2,height=2),month='2020-09')
    result=api().relate_components([a,b])
    assert result['relations'][0]['relation_kind']=='cross_month_spatial_adjacency'
    assert result['relations'][0]['overlap_pixels']==0
    assert result['relations'][0]['reburn_relation'] is None


def test_sparse_components_have_only_their_actual_cross_core_relations():
    a=index([[0,1],[0,0],[0,0],[0,1]],g=grid(width=2,height=4))
    b=index([[1,0],[0,0],[0,0],[1,0]],g=grid(left=500060,width=2,height=4))
    result=api().relate_components([a,b])
    assert len(result['relations'])==2
    assert len({r['left_component_id'] for r in result['relations']})==2
    assert len({r['right_component_id'] for r in result['relations']})==2


def test_mask_and_index_tampering_cannot_be_accepted_as_primary_support():
    a=index([[1,0],[0,0]],g=grid(width=2,height=2))
    changed=deepcopy(a);changed['mask']['valid_bits']='AA=='
    with pytest.raises(ValueError,match='mask SHA'):
        api().decode_masks(changed)
    changed=deepcopy(a);changed['candidate_area_m2']=1800
    with pytest.raises(ValueError,match='index SHA'):
        api().relate_components([changed])


def test_different_models_are_not_linked_as_one_candidate_system():
    a=index([[0,1],[0,0]],g=grid(width=2,height=2))
    b=index([[1,0],[0,0]],g=grid(left=500060,width=2,height=2),model='new-model')
    result=api().relate_components([a,b])
    assert result['status']=='unavailable' and result['relations']==[]
    assert result['unavailable_reasons']==['different_models']


def test_array_cores_crossing_antimeridian_are_not_falsely_supported():
    from pyproj import Transformer
    x,y=Transformer.from_crs('EPSG:4326','EPSG:32660',always_xy=True).transform(179.9999,1)
    g=grid(left=x,top=y,width=2,height=2);g['crs']='EPSG:32660'
    with pytest.raises(ValueError,match='antimeridian'):
        index([[1,0],[0,0]],g=g)


def test_saved_and_explicit_array_inputs_share_component_identity_for_same_physical_grid(tmp_path):
    import rasterio
    from affine import Affine
    values=np.array([[1,0],[0,1]],dtype='float32');g=grid(width=2,height=2)
    path=tmp_path/'same-grid.tif'
    with rasterio.open(path,'w',driver='GTiff',width=2,height=2,count=1,dtype='float32',
        crs=g['crs'],transform=Affine(*g['transform'])) as dst:
        dst.write(values,1);dst.set_band_description(1,'candidate')
    saved=api().index_saved_candidates(path,model_id='frozen-model',month='2020-08',calibration_status='uncalibrated_rf_candidate')
    direct=index(values,g=g)
    assert saved['components']==direct['components']
