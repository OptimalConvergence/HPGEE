"""Offline contracts for one validated cohort and many lazy prediction graphs."""
from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import ee
from ee import apitestcase
import pytest

from geecomputation.config import Config
from geecomputation.features import DEM_BANDS, RF_BANDS, feature_image
from geecomputation import rf
from .test_contract import configuration, row


@pytest.fixture(autouse=True)
def offline_ee():
    helper = apitestcase.ApiTestCase(); helper.setUp()
    yield
    helper.tearDown(); ee.Reset()


def config_and_split(tmp_path):
    split = tmp_path / 'split.json'
    split.write_text(json.dumps({'events': {'one': 'train'}, 'supports': {'one': 'train'}}))
    raw = configuration()
    raw['labels'] = {'frozen_split_manifest': str(split), 'max_samples': 5}
    return Config.from_dict(raw), split


def no_request(graph):
    pytest.fail('frozen model/prediction construction must not request EE values')


def test_snapshot_survives_remote_rows_and_split_mutation_without_revalidation(tmp_path, monkeypatch):
    cfg, split = config_and_split(tmp_path)
    records = [{'type': 'Feature', 'geometry': None, 'properties': row(label)} for label in (0, 1)]
    monkeypatch.setattr(ee.data, 'computeValue', lambda graph: records)
    asset = 'projects/example/assets/MUTABLE_SOURCE'
    snapshot = rf.freeze_training_snapshot(ee.FeatureCollection(asset), cfg)
    split_sha = hashlib.sha256(split.read_bytes()).hexdigest()
    records[0]['properties']['label'] = 1
    split.write_text('{}')
    monkeypatch.setattr(ee.data, 'computeValue', no_request)
    monkeypatch.setattr(rf, 'validate_samples', lambda *args, **kwargs: pytest.fail('cohort was already validated'))
    model, metadata = rf.build_rf_model(snapshot, cfg)
    changed_roi = replace(cfg, bbox=(30.01, -2, 30.05, -1.95))
    scores = [rf.predict_rf(model, item) for item in (cfg, changed_roi)]
    assert all(json.loads(score.serialize())['result'] for score in scores)
    assert asset not in model.serialize() and 'FeatureCollection.loadTable' not in model.serialize()
    assert metadata['sample_count'] == 2 and metadata['class_order'] == [0, 1]
    assert metadata['frozen_split_manifest'] == str(split)
    assert metadata['frozen_split_manifest_sha256'] == split_sha


def test_local_snapshot_needs_no_ee_roundtrip_and_freezes_file_contents(tmp_path, monkeypatch):
    cfg, split = config_and_split(tmp_path)
    source = tmp_path / 'samples.json'
    source.write_text(json.dumps({'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'geometry': None, 'properties': row(label)} for label in (0, 1)]}))
    cfg = replace(cfg, labels={**cfg.labels, 'local_samples': str(source)})
    monkeypatch.setattr(ee.data, 'computeValue', no_request)
    snapshot = rf.freeze_training_snapshot(None, cfg)
    source.unlink(); split.unlink()
    model, metadata = rf.build_rf_model(snapshot, cfg)
    assert json.loads(model.serialize())['result']
    assert metadata['sample_count'] == 2


def test_snapshot_is_immutable_and_model_rejects_unvalidated_dict(tmp_path, monkeypatch):
    cfg, _ = config_and_split(tmp_path)
    monkeypatch.setattr(ee.data, 'computeValue', lambda graph: [
        {'type': 'Feature', 'geometry': None, 'properties': row(label)} for label in (0, 1)])
    snapshot = rf.freeze_training_snapshot(ee.FeatureCollection([]), cfg)
    with pytest.raises(FrozenInstanceError):
        snapshot.sample_snapshot_sha256 = 'forged'
    with pytest.raises(TypeError, match='TrainingSnapshot'):
        rf.build_rf_model({'rows': [row(0), row(1)]}, cfg)


def test_snapshot_constructor_cannot_bypass_sample_validation(tmp_path):
    cfg, split = config_and_split(tmp_path)
    with pytest.raises(ValueError, match='class'):
        rf.TrainingSnapshot(rows_json=(json.dumps(row(0)),), feature_bands=RF_BANDS,
                            split_manifest_payload=split.read_bytes(),
                            frozen_split_manifest=str(split))
    with pytest.raises(TypeError, match='immutable'):
        rf.TrainingSnapshot(rows_json=[json.dumps(row(0)), json.dumps(row(1))],
                            feature_bands=RF_BANDS, split_manifest_payload=split.read_bytes(),
                            frozen_split_manifest=str(split))


def test_frozen_columns_reject_missing_detection_features(tmp_path, monkeypatch):
    cfg, _ = config_and_split(tmp_path)
    monkeypatch.setattr(ee.data, 'computeValue', lambda graph: [
        {'type': 'Feature', 'geometry': None, 'properties': row(label)} for label in (0, 1)])
    snapshot = rf.freeze_training_snapshot(ee.FeatureCollection([]), cfg)
    dem_cfg = replace(cfg, rf={'trees': 100, 'feature_set': 'core6_dem'})
    with pytest.raises(ValueError, match='feature'):
        rf.build_rf_model(snapshot, dem_cfg)
    with pytest.raises(ValueError, match='feature'):
        rf.freeze_training_snapshot(ee.FeatureCollection([]), cfg, snapshot_bands=RF_BANDS[:-1])


def test_superset_snapshot_reuses_cohort_for_model_variants_without_feature_build(tmp_path, monkeypatch):
    import geecomputation.features as features
    cfg, split = config_and_split(tmp_path)
    monkeypatch.setattr(ee.data, 'computeValue', lambda graph: [
        {'type': 'Feature', 'geometry': None, 'properties': dict(row(label), **dict.fromkeys(DEM_BANDS, .1))}
        for label in (0, 1)])
    snapshot = rf.freeze_training_snapshot(ee.FeatureCollection([]), cfg, snapshot_bands=RF_BANDS + DEM_BANDS)
    split.unlink()
    monkeypatch.setattr(ee.data, 'computeValue', no_request)
    monkeypatch.setattr(features, 'feature_image', lambda cfg: pytest.fail('model construction cannot build features'))
    hashes = []
    for trees, feature_set, count in ((50, 'core6', 6), (200, 'core6_dem', 10)):
        model, metadata = rf.build_rf_model(snapshot, replace(cfg, rf={'trees': trees, 'feature_set': feature_set}))
        assert json.loads(model.serialize())['result']
        assert metadata['trees'] == trees and len(metadata['features']) == count
        hashes.append(metadata['sample_snapshot_sha256'])
    assert hashes[0] == hashes[1]


def test_snapshot_rejects_frozen_split_mismatch_and_expected_hash(tmp_path, monkeypatch):
    cfg, split = config_and_split(tmp_path)
    monkeypatch.setattr(ee.data, 'computeValue', lambda graph: [
        {'type': 'Feature', 'geometry': None, 'properties': row(label)} for label in (0, 1)])
    with pytest.raises(ValueError, match='hash'):
        rf.freeze_training_snapshot(ee.FeatureCollection([]), replace(cfg, labels={
            **cfg.labels, 'expected_sample_sha256': '0' * 64}))
    split.write_text(json.dumps({'events': {'other': 'train'}, 'supports': {'one': 'train'}}))
    with pytest.raises(ValueError, match='manifest'):
        rf.freeze_training_snapshot(ee.FeatureCollection([]), cfg)


def test_prediction_accepts_explicit_feature_graph_and_requires_single_source(tmp_path, monkeypatch):
    cfg, _ = config_and_split(tmp_path)
    monkeypatch.setattr(ee.data, 'computeValue', lambda graph: [
        {'type': 'Feature', 'geometry': None, 'properties': row(label)} for label in (0, 1)])
    snapshot = rf.freeze_training_snapshot(ee.FeatureCollection([]), cfg)
    model, _ = rf.build_rf_model(snapshot, cfg)
    monkeypatch.setattr(ee.data, 'computeValue', no_request)
    features = ee.Image.constant([.1] * 6).rename(list(RF_BANDS))
    expected = features.classify(model).arrayFlatten([['score_unburned', 'score_burned']])
    assert rf.predict_rf(model, features=features).serialize() == expected.serialize()
    with pytest.raises(ValueError, match='exactly one'):
        rf.predict_rf(model)
    with pytest.raises(ValueError, match='exactly one'):
        rf.predict_rf(model, cfg, features=features)


def test_train_rf_keeps_default_graph_and_metadata_contract(tmp_path, monkeypatch):
    cfg, split = config_and_split(tmp_path)
    rows = [row(0), row(1)]
    monkeypatch.setattr(ee.data, 'computeValue', lambda graph: [
        {'type': 'Feature', 'geometry': None, 'properties': item} for item in rows])
    training = ee.FeatureCollection([ee.Feature(None, item) for item in rows])
    expected_model = ee.Classifier.smileRandomForest(numberOfTrees=100, seed=42).setOutputMode(
        'MULTIPROBABILITY').train(features=training, classProperty='label', inputProperties=list(RF_BANDS))
    expected_scores = feature_image(cfg).classify(expected_model).arrayFlatten([['score_unburned', 'score_burned']])
    model, scores, metadata = rf.train_rf(ee.FeatureCollection([]), cfg)
    canonical = sorted(json.dumps(item, sort_keys=True, separators=(',', ':'), allow_nan=False) for item in rows)
    expected_sha = hashlib.sha256(json.dumps(canonical, separators=(',', ':')).encode()).hexdigest()
    assert model.serialize() == expected_model.serialize()
    assert scores.serialize() == expected_scores.serialize()
    assert metadata == {'class_order': [0, 1], 'class_names': ['unburned', 'burned'],
                        'score_calibrated': False, 'trees': 100, 'feature_set': 'core6', 'seed': 42,
                        'frozen_split_manifest': str(split),
                        'frozen_split_manifest_sha256': hashlib.sha256(split.read_bytes()).hexdigest(),
                        'sample_snapshot_sha256': expected_sha,
                        'training_input': 'validated literal rows; geometry omitted; no remote asset dependency',
                        'sample_count': 2, 'features': list(RF_BANDS)}
