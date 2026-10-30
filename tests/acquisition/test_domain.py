import json
from types import SimpleNamespace
import numpy as np
from hpgee.acquisition import ee_quality
from hpgee.acquisition.quality import cache_key


def test_quality_cache_cannot_reuse_counts_from_another_ownership_domain(tmp_path):
    task = dict(
        sensor="S30",
        sources=[{"scene": {"asset_id": "A"}}],
        grid={},
        land_asset="land",
        observation="o",
    )
    owner = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    common = dict(
        sources=task["sources"],
        grid={},
        sensor="S30",
        scale=30,
        land_asset="land",
        observation="o",
        count_engine="fixed-land-grid-v3",
    )
    old = cache_key(**common)
    new = cache_key(**common, owner_geometry=owner)
    for key, n in ((old, 16384), (new, 8192)):
        (tmp_path / (key + ".json")).write_text(json.dumps(dict(key=key, rows=[dict(valid=n)])))
    assert (
        ee_quality.collect(None, dict(task, owner_geometry=owner), 30, tmp_path)["rows"][0]["valid"]
        == 8192
    )
    assert ee_quality.collect(None, task, 30, tmp_path)["rows"][0]["valid"] == 16384


def test_domain_is_land_intersected_with_this_observations_owner(monkeypatch):
    land = np.array([[1, 0], [1, 1]], dtype=bool)
    owner = [[0, 1], [1, 0]]

    class Image:
        def __init__(self, value):
            self.data = np.full(land.shape, value) if np.isscalar(value) else np.asarray(value)

        def byte(self):
            return self

        def paint(self, *a):
            return Image(land)

        def clip(self, mask):
            return Image(self.data & np.asarray(mask, dtype=bool))

        def unmask(self, *a):
            return self

        def reproject(self, crs, affine):
            self.projection = (crs, affine)
            return self

    ee = SimpleNamespace(
        Image=Image,
        FeatureCollection=lambda _: SimpleNamespace(filterBounds=lambda _: None),
        Geometry=lambda x: x,
    )
    monkeypatch.setattr(ee_quality, "geometry", lambda *a: None)
    grid = dict(
        crsCode="EPSG:32648",
        affineTransform=dict(scaleX=30, scaleY=-30, translateX=0, translateY=0),
    )
    mask = ee_quality.domain_mask(ee, dict(grid=grid, land_asset="land", owner_geometry=owner))
    assert mask.projection == ("EPSG:32648", [30, 0, 0, 0, -30, 0])
    assert np.array_equal(mask.data, land & np.asarray(owner, dtype=bool))
