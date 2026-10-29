import copy
from datetime import timezone
import json
from pathlib import Path

import numpy as np
import pytest

from hpgee.workflows.config import Config, utc_datetime
from hpgee.compute.features import clear_fmask, spectral_indices, min_nbr2_pixels, rf_features
from hpgee.compute.rf import validate_samples
from hpgee.workflows.recipe import manifest


def configuration():
    return dict(project='example-project', bbox=[30, -2, 30.05, -1.95],
                start='2020-08-01', end='2020-09-01', pre_start='2020-07-01', pre_end='2020-08-01',
                grid=dict(crs='EPSG:32736', transform=[30, 0, 166000, 0, -30, 9785000], width=128, height=128),
                output='outputs', budget_eecu_hours=100, seed=42)


def test_utc_half_open_and_grid_contract():
    cfg = Config.from_dict(configuration())
    assert cfg.start == '2020-08-01T00:00:00Z'
    assert utc_datetime('2020-08-01T08:00:00+08:00').hour == 0
    assert utc_datetime('2020-08-01').tzinfo == timezone.utc
    for key, value in [('end', '2020-08-01'), ('budget_eecu_hours', -1), ('seed', 2.5)]:
        raw = configuration(); raw[key] = value
        with pytest.raises(ValueError): Config.from_dict(raw)
    raw = configuration(); raw['grid']['transform'][0] = 10
    with pytest.raises(ValueError): Config.from_dict(raw)
    raw = configuration(); raw['bbox'] = [-180, -89, 180, 89]
    with pytest.raises(ValueError): Config.from_dict(raw)


def test_fmask_cloud_adjacency_shadow_snow_and_aerosol():
    # bit 0 reserved; water retained; aerosol high rejected, moderate retained.
    values = np.array([0, 1, 2, 4, 8, 16, 32, 64, 128, 192, 255])
    assert clear_fmask(values).tolist() == [True, True, False, False, False, False, True, True, True, False, False]


def test_negative_reflectance_is_not_implicitly_masked_and_zero_sum_is_unknown():
    nbr2, mirbi = spectral_indices(np.array([-.1, .1, 0]), np.array([.3, -.1, 0]))
    assert nbr2[0] == pytest.approx(-2)
    assert np.isnan(nbr2[1:]).all()
    assert mirbi[0] == pytest.approx(5.98)


def test_minimum_nbr2_selects_all_bands_and_time_from_one_scene():
    # (scene, pixel, common6): independent minima; all-invalid pixel stays unknown.
    data = np.array([[[1,2,3,4,.6,.2],[11,12,13,14,.2,.6],[1,1,1,1,.3,.1]],
                     [[5,6,7,8,.1,.3],[15,16,17,18,.6,.2],[2,2,2,2,.1,.3]]], dtype=float)
    chosen, times = min_nbr2_pixels(data, [1000,2000], np.array([[1,1,0],[1,1,0]], dtype=bool))
    assert chosen[0].tolist() == [5,6,7,8,.1,.3]
    assert chosen[1].tolist() == [11,12,13,14,.2,.6]
    assert times[:2].tolist() == [2000,1000]
    assert np.isnan(chosen[2]).all() and np.isnan(times[2])


def test_empty_optical_collection_is_unknown():
    chosen,times=min_nbr2_pixels(np.empty((0,3,6)),[],np.empty((0,3),dtype=bool))
    assert chosen.shape==(3,6) and np.isnan(chosen).all()
    assert times.shape==(3,) and np.isnan(times).all()


def test_rf_delta_sign_and_missing_optional_modalities():
    pre = np.array([.1,.1,.1,.5,.6,.2]); post = np.array([.1,.1,.1,.2,.2,.6])
    features = rf_features(pre, post)
    assert features.tolist() == pytest.approx([-.5,6.04,-1,7.92,-.3,-.4])
    assert features.shape == (6,)


def row(label, split='train', event='one', support='one', source='manual'):
    from hpgee.compute.features import RF_BANDS
    return dict(label=label, split=split, event_id=event, support_id=support,
                label_source=source, label_eligible=True, **dict.fromkeys(RF_BANDS, .1))


def test_rf_requires_classes_and_disjoint_event_support():
    assert validate_samples([row(0), row(1)]) == [0,1]
    with pytest.raises(ValueError, match='class'): validate_samples([row(0)])
    with pytest.raises(ValueError, match='event'): validate_samples([row(0),row(1),row(0,'test')])
    with pytest.raises(ValueError, match='support'): validate_samples([row(0),row(1),row(0,'test','two')])
    with pytest.raises(ValueError, match='weak'): validate_samples([row(0),row(1),row(0,'test','two','two','weak')])
    with pytest.raises(ValueError): validate_samples([row(-1),row(1)])


def test_manifest_identifies_scene_key_scale_and_exact_grid():
    result = manifest(Config.from_dict(configuration()))
    assert result['reflectance_scale'] == 1.0
    assert result['dedup_key'] == ['source_collection','source_index']
    assert result['grid']['affineTransform']['scaleY'] == -30
    assert result['interval_utc']['end'] == '2020-09-01T00:00:00Z'
    assert result['bands'][-1] == 'time_ms'


def test_frozen_manifest_requires_files_and_matching_groups(tmp_path):
    from hpgee.compute.rf import load_split_manifest
    path=tmp_path/'split.json'
    with pytest.raises(FileNotFoundError): load_split_manifest(path)
    path.write_text(json.dumps({'events':{'one':'train'},'supports':{'one':'train'}}))
    frozen,sha=load_split_manifest(path)
    assert len(sha)==64
    assert validate_samples([row(0),row(1)],split_manifest=frozen)==[0,1]
    with pytest.raises(ValueError,match='manifest'):
        validate_samples([row(0),row(1,event='unknown')],split_manifest=frozen)
    with pytest.raises(ValueError,match='manifest'):
        validate_samples([row(0),row(1,split='test')],split_manifest=frozen)


def test_manifest_binds_pre_roi_and_rf_contract():
    a=manifest(Config.from_dict(configuration()))
    raw=configuration(); raw['pre_start']='2020-06-01'
    b=manifest(Config.from_dict(raw))
    assert a['config_sha256']!=b['config_sha256']
    assert a['rf']['feature_set']=='core6' and a['rf']['trees']==100
    assert a['roi_bbox']==[30,-2,30.05,-1.95]
    assert a['detection_inputs']==['HLS_common6']
    assert 'GEDI' in a['process_aux']


def test_optional_tile_whitelist_declares_limited_selection_scope():
    raw=configuration(); raw['hls_tile_ids']=['T35MRT']
    cfg=Config.from_dict(raw); result=manifest(cfg)
    assert cfg.hls_tile_ids==('T35MRT',)
    assert result['selection_scope']['hls_tile_ids']==['T35MRT']
    assert result['selection_scope']['cannot_claim_enumeration_completeness'] is True
    raw['hls_tile_ids']=['UTM35']
    with pytest.raises(ValueError): Config.from_dict(raw)


def test_native_extent_filter_is_explicit_not_an_assumed_global_default():
    cfg=Config.from_dict(configuration())
    assert cfg.hls_native_dimensions==()
    raw=configuration(); raw['hls_native_dimensions']=[3660,3660]
    cfg=Config.from_dict(raw)
    assert manifest(cfg)['selection_scope']['native_extent_dimensions']==[3660,3660]
    raw['hls_native_dimensions']=[3660]
    with pytest.raises(ValueError): Config.from_dict(raw)


def test_local_training_source_freezes_sha_and_rejects_ambiguous_or_large_inputs(tmp_path):
    from hpgee.compute.rf import sample_source_info
    path=tmp_path/'samples.json'
    payload={'type':'FeatureCollection','features':[{'type':'Feature','geometry':None,'properties':row(0)},
                                                    {'type':'Feature','geometry':None,'properties':row(1)}]}
    path.write_text(json.dumps(payload)); raw=configuration()
    raw['labels']={'local_samples':str(path),'max_samples':2}
    cfg=Config.from_dict(raw)
    info=sample_source_info(cfg)
    assert info['kind']=='local_feature_collection' and len(info['sha256'])==64
    assert manifest(cfg)['local_samples_sha256']==info['sha256']
    raw['labels']['asset']='projects/example/assets/samples'
    with pytest.raises(ValueError,match='one'): sample_source_info(Config.from_dict(raw))
    del raw['labels']['asset']; payload['features'].append(payload['features'][0]); path.write_text(json.dumps(payload))
    with pytest.raises(ValueError,match='cap'): sample_source_info(Config.from_dict(raw))
