"""Cross-workflow grid interoperability without Earth Engine calls."""
import json
from pathlib import Path

import pytest

from hpgee.core.grid import ComputedGrid, PixelGrid
from hpgee.transfer.raw_download import grid_spec
from hpgee.workflows.config import Config


def test_sampling_workflow_and_raw_transfer_share_exact_pixel_support():
    raw = json.loads((Path(__file__).parents[1] / 'examples/computation/small-roi.json').read_text())
    config = Config.from_dict(raw)
    a, _, x, _, e, y = config.grid['transform']
    computed = ComputedGrid(config.grid['crs'], config.grid['width'], config.grid['height'], a, e, x, y)
    assert computed.to_ee() == config.pixel_grid()
    assert grid_spec(computed.to_ee()) == {'crs': config.grid['crs'], 'transform': config.grid['transform']}


@pytest.mark.parametrize('field,crs', [('crsCode', 'EPSG:32610'), ('crsWkt', 'LOCAL_CS["test"]')])
def test_rectangular_sheared_grid_is_lossless(field, crs):
    source = {'dimensions': {'width': 11, 'height': 7}, field: crs,
              'affineTransform': {'scaleX': 30, 'shearX': 1.25, 'translateX': 123.456789012345,
                                  'shearY': -0.75, 'scaleY': -30, 'translateY': 456.789012345678}}
    grid = PixelGrid.from_ee(source)
    assert grid.to_ee() == source
    assert grid_spec(source)['transform'] == [30, 1.25, 123.456789012345, -0.75, -30, 456.789012345678]


def test_shared_grid_does_not_relax_workflow_scientific_contract():
    raw = json.loads((Path(__file__).parents[1] / 'examples/computation/small-roi.json').read_text())
    raw['grid']['transform'][1] = 2
    assert PixelGrid.from_config(raw['grid']).transform[1] == 2
    with pytest.raises(ValueError, match='north-up 30 m'):
        Config.from_dict(raw)


@pytest.mark.parametrize('transform', [(0, 0, 0, 0, 0, 0), (30, 0, float('nan'), 0, -30, 0)])
def test_invalid_grid_fails_before_any_transport(transform):
    with pytest.raises(ValueError):
        PixelGrid('EPSG:32610', transform, 8, 4)


@pytest.mark.parametrize("field,crs", [("crsCode", "EPSG:32610"), ("crsWkt", 'LOCAL_CS["test"]')])
def test_configuration_roundtrip_preserves_ee_crs_representation(field, crs):
    grid = PixelGrid(crs, (30, 0, 500000, 0, -30, 4100000), 10, 20, field)
    config = grid.to_config()
    assert PixelGrid.from_config(config).to_ee() == grid.to_ee()
    if field == "crsCode":
        assert "crs_field" not in config
