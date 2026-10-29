"""Real GEESampler transport/ledger with a known product and no metadata RPC."""
import json
import sqlite3

import ee
from ee import apitestcase
import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from affine import Affine

from hpgee.transfer.engine import DownloadEngine
from hpgee.core.models import RunConfig, EECUMonitorConfig, SceneSelection
from hpgee.workflows.config import Config
import hpgee.transfer.products as transfer
from hpgee.compute.features import RF_BANDS, DEM_BANDS
from hpgee.workflows.detection import PRODUCT_BANDS, workflow_contract, finalize_transfer, boundaries_from_saved_raster, verify_artifacts, build_products


@pytest.fixture(autouse=True)
def offline_ee():
    helper = apitestcase.ApiTestCase(); helper.setUp()
    yield
    helper.tearDown(); ee.Reset()


def fixture(aux_context=False):
    cfg = Config.from_dict(dict(project='test-project', bbox=[3, 8.99, 3.01, 9.01],
        start='2020-08-01', end='2020-09-01', pre_start='2020-07-01', pre_end='2020-08-01',
        grid=dict(crs='EPSG:32631', transform=[30, 0, 500000, 0, -30, 1000000], width=2, height=2),
        seed=42, output='unused', budget_eecu_hours=1))
    training = ee.FeatureCollection([ee.Feature(None, dict(label=label, **dict.fromkeys(RF_BANDS, .1))) for label in (0, 1)])
    model = ee.Classifier.smileRandomForest(numberOfTrees=100, seed=42).setOutputMode('MULTIPROBABILITY').train(
        features=training, classProperty='label', inputProperties=list(RF_BANDS))
    scores = ee.Image.constant([.1] * 6).rename(list(RF_BANDS)).classify(model).arrayFlatten([list(PRODUCT_BANDS[:2])])
    if aux_context:
        aux = ee.Image.constant([1, 2, 3, 4]).rename(list(DEM_BANDS))
        return cfg, build_products(cfg, scores, aux=aux, aux_context_sha256='a' * 64)
    raster = scores.addBands(ee.Image.constant([.1] * 19).rename(list(PRODUCT_BANDS[2:]))).toDouble().updateMask(ee.Image(1))
    return cfg, dict(raster=raster, bands=list(PRODUCT_BANDS), contract=workflow_contract(cfg, .5))


def geotiff():
    values = np.ones((21, 2, 2), dtype='float64'); values[:, 0, 0] = np.nan
    for name, value in [('score_unburned', .9), ('score_burned', .1), ('candidate', 0),
                        ('pre_time_ms', 1593561600000), ('post_time_ms', 1596240000000),
                        ('pre_Fmask', 0), ('post_Fmask', 0)]:
        values[PRODUCT_BANDS.index(name), 1:, :] = value
        values[PRODUCT_BANDS.index(name), 0, 1:] = value
    with MemoryFile() as file:
        with file.open(driver='GTiff', width=2, height=2, count=21, dtype='float64',
                       crs='EPSG:32631', transform=Affine(30, 0, 500000, 0, -30, 1000000), nodata=np.nan) as dst:
            dst.write(values)
        return file.read()


@pytest.mark.parametrize('aux_context', [False, True])
def test_real_sampler_transfers_one_product_without_any_metadata_read_and_preserves_mask(tmp_path, monkeypatch, aux_context):
    module = transfer; cfg, products = fixture(aux_context)
    def forbidden(*args, **kwargs):
        pytest.fail('fixed product must not request metadata')
    for name in ('computeValue', 'computeFeatures', 'listImages', 'getAsset', 'getInfo', 'getList'):
        monkeypatch.setattr(ee.data, name, forbidden)
    requests = []; payload = geotiff()
    def compute(request):
        requests.append(request)
        assert request['bandIds'] == list(PRODUCT_BANDS) and request['grid'] == cfg.pixel_grid()
        graph = request['expression'].serialize()
        assert 'workflow_product' in graph and '1596240000000' in graph
        assert 'Classifier.train' in graph
        # Real workflow fills valid_count=0; transfer must add no unmask of its own.
        assert graph.count('"Image.unmask"') == products['raster'].serialize().count('"Image.unmask"')
        return payload
    monkeypatch.setattr(ee.data, 'computePixels', compute)
    engine = DownloadEngine(cfg.project, RunConfig(output_dir=tmp_path, workers=1, retries=0,
        eecu=EECUMonitorConfig(enabled=False)), ee_module=ee)
    arguments = module.product_sampler_arguments(cfg, products)
    summary = engine.download_patch_series(**arguments, scenario='product', run_id='fixed')
    assert summary.succeeded == 1 and summary.failed == 0 and len(requests) == 1
    result = summary.results[0]
    assert result.scene_id == 'workflow_product'
    assert result.scene_date == result.target_date == '2020-08-01T00:00:00+00:00'
    assert result.sample_properties['time_role'] == 'derived_product_reference_time'
    assert 'scene_selection' not in result.timings and 'compute_pixels' in result.timings
    assert arguments['scene_resolver'].stats().metadata_fallbacks == 0
    with sqlite3.connect(tmp_path / 'fixed' / 'ledger.sqlite') as ledger:
        assert ledger.execute("SELECT count(*) FROM tasks WHERE status='success'").fetchone()[0] == 1
    packaged = tmp_path / 'named.tif'
    finalize_transfer(result.output_path, packaged, products['contract'], transfer_bands=arguments['bands'])
    with rasterio.open(packaged) as ds:
        assert ds.descriptions == PRODUCT_BANDS and ds.dtypes == ('float64',) * 21
        assert np.ma.getmaskarray(ds.read(masked=True))[:, 0, 0].all()
    boundary = tmp_path / 'boundary.geojson'
    boundary.write_text(json.dumps(boundaries_from_saved_raster(packaged)))
    verification = verify_artifacts(packaged, boundary, products['contract'])
    assert verification['observed_pixels'] == 3 and verification['candidate_pixels'] == 0
    resumed = engine.download_patch_series(**arguments, scenario='product', run_id='fixed')
    assert resumed.skipped == 1 and resumed.bytes_downloaded == 0 and len(requests) == 1


@pytest.mark.parametrize('change', ['bands', 'grid', 'threshold', 'source'])
def test_changed_product_contract_is_rejected_before_transport(change):
    module = transfer; cfg, products = fixture()
    if change == 'bands': products['bands'] = products['bands'][::-1]
    elif change == 'grid': products['contract']['grid']['dimensions']['width'] = 3
    elif change == 'threshold': products['contract']['threshold'] = .6
    else: products['contract']['source_manifest_sha256'] = 'changed'
    with pytest.raises(ValueError, match='contract'):
        module.product_sampler_arguments(cfg, products)


@pytest.mark.parametrize('change', ['missing', 'invalid', 'type', 'changed', 'selection'])
def test_changed_aux_context_contract_is_rejected_before_transport(change, monkeypatch):
    cfg, products = fixture(aux_context=True)
    for name in ('computeValue', 'computeFeatures', 'computePixels'):
        monkeypatch.setattr(ee.data, name, lambda *a, **kw: pytest.fail('invalid aux contract requested EE'))
    if change == 'missing': products['contract'].pop('aux_context_sha256')
    elif change == 'invalid': products['contract']['aux_context_sha256'] = 'bad'
    elif change == 'type': products['contract']['aux_context_sha256'] = True
    elif change == 'changed': products['contract']['aux_context_sha256'] = 'b' * 64
    else: products['contract']['aux']['DEM']['selection'] = 'dynamic'
    with pytest.raises(ValueError, match='contract'):
        transfer.product_sampler_arguments(cfg, products)


@pytest.mark.parametrize('selection', [SceneSelection(), SceneSelection(mode='all', start_offset_days=0, end_offset_days=1),
                                    SceneSelection(mode='all', start_offset_days=0, end_offset_days=0, max_scenes=2)])
def test_resolver_rejects_dynamic_selection_before_any_metadata_or_pixels(tmp_path, monkeypatch, selection):
    module = transfer; cfg, products = fixture(); arguments = module.product_sampler_arguments(cfg, products)
    monkeypatch.setattr(ee.data, 'computeValue', lambda *a: pytest.fail('metadata requested'))
    monkeypatch.setattr(ee.data, 'computePixels', lambda *a: pytest.fail('pixels requested'))
    engine = DownloadEngine(cfg.project, RunConfig(output_dir=tmp_path, workers=1, retries=0,
        eecu=EECUMonitorConfig(enabled=False)), ee_module=ee)
    arguments['selection'] = selection
    with pytest.raises(ValueError, match='selection'):
        engine.download_patch_series(**arguments, scenario='product', run_id='invalid')


def test_callback_sets_derived_product_clock_without_changing_source_time_bands(monkeypatch):
    module = transfer; cfg, products = fixture()
    original = products['raster'].serialize()
    monkeypatch.setattr(ee.data, 'computeValue', lambda *a: pytest.fail('metadata requested'))
    arguments = module.product_sampler_arguments(cfg, products)
    collection = arguments['collection_builder'](arguments['records'][0])
    graph = collection.serialize()
    assert 'workflow_product' in graph and '1596240000000' in graph
    assert products['raster'].serialize() == original
    assert arguments['selection'] == SceneSelection(mode='all', start_offset_days=0, end_offset_days=0, max_scenes=1)
    assert not arguments['scene_resolver'].inline_quality and not arguments['scene_resolver'].probe_quality
    assert not arguments['scene_resolver'].cloud_masking
    assert products['contract'] == workflow_contract(cfg, .5)
