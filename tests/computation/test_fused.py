"""Offline contract checks for the independent HLS–S1–DEM fused recipe."""
import json

import ee
import pytest
from ee import apitestcase

from geecomputation.fused import (FEATURE_BANDS, FEATURE_CONTRACT, QA_BANDS,
                            feature_image, followup_evidence, month_windows,
                            predict_month, train_rf)


@pytest.fixture(autouse=True)
def offline_ee():
    helper = apitestcase.ApiTestCase()
    helper.setUp()
    yield
    helper.tearDown()
    ee.Reset()


def test_utc_month_windows_cross_year_and_leap_day():
    assert month_windows(2019, 1) == {
        "pre": ("2018-12-01", "2019-01-01"),
        "post": ("2019-01-01", "2019-02-01"),
        "follow": ("2019-02-01", "2019-03-01")}
    assert month_windows(2020, 2)["post"] == ("2020-02-01", "2020-03-01")
    assert month_windows(2019, 12)["follow"] == ("2020-01-01", "2020-02-01")
    with pytest.raises(ValueError):
        month_windows(2019, 13)


def test_fused_graph_keeps_products_out_and_explicit_s1_missing():
    region = ee.Geometry.Rectangle([20, 36, 21, 37])
    result = feature_image(region, 2019, 4, orbit_pass="ASCENDING", relative_orbit=34,
                           grid_crs="EPSG:32634", grid_transform=[30, 0, 400000, 0, -30, 4100000])
    assert result["feature_contract"] == FEATURE_CONTRACT
    assert result["bands"] == FEATURE_BANDS
    assert len(FEATURE_BANDS) == 29 and len(set(FEATURE_BANDS)) == 29
    assert len(QA_BANDS) == 8 and "s1_missing" in FEATURE_BANDS
    graph = json.loads(result["predictors"].serialize())
    as_text = json.dumps(graph)
    for source in ("NASA/HLS/HLSL30/v002", "NASA/HLS/HLSS30/v002",
                   "COPERNICUS/S1_GRD", "COPERNICUS/DEM/GLO30_2024_1"):
        assert source in as_text
    for forbidden in ("MODIS/061/MCD64A1", "ESA/CCI/FireCCI/5_1"):
        assert forbidden not in as_text
    assert json.loads(result["qa"].serialize())["result"]
    follow = followup_evidence(region, 2019, 4)
    follow_graph = json.dumps(json.loads(follow.serialize()))
    assert "follow_nbr" in follow_graph and "hls_follow_count" in follow_graph
    assert "MODIS/061/MCD64A1" not in follow_graph


def test_invalid_orbit_grid_and_threshold_rejected_before_server():
    region = ee.Geometry.Point([20.5, 36.5])
    kw = {"orbit_pass": "ASCENDING", "relative_orbit": 34,
          "grid_crs": "EPSG:32634", "grid_transform": [30, 0, 400000, 0, -30, 4100000]}
    with pytest.raises(ValueError):
        feature_image(region, 2019, 4, **{**kw, "orbit_pass": "ANY"})
    with pytest.raises(ValueError):
        feature_image(region, 2019, 4, **{**kw, "relative_orbit": 0})
    with pytest.raises(ValueError):
        feature_image(region, 2019, 4, **{**kw, "grid_transform": [30, 0]})
    with pytest.raises(ValueError):
        predict_month(None, None, threshold=0)


def test_lazy_100_tree_classifier_and_candidate_graph_serialize():
    rows = [ee.Feature(None, {**dict.fromkeys(FEATURE_BANDS, float(label)), "label": label})
            for label in (0, 1)]
    model = train_rf(ee.FeatureCollection(rows))
    assert json.loads(model.serialize())["result"]
    predicted = predict_month(model, {"predictors": ee.Image.constant([0] * len(FEATURE_BANDS))
                                       .rename(list(FEATURE_BANDS)),
                                      "qa": ee.Image.constant([1] * len(QA_BANDS)).rename(list(QA_BANDS))},
                              threshold=0.5)
    assert json.loads(predicted.serialize())["result"]
    with pytest.raises(ValueError):
        train_rf(ee.FeatureCollection(rows), trees=200)


def test_prediction_output_carries_s1_missing_flag():
    rows = [ee.Feature(None, {**dict.fromkeys(FEATURE_BANDS, float(label)), "label": label})
            for label in (0, 1)]
    predictors = ee.Image.constant([0] * len(FEATURE_BANDS)).rename(list(FEATURE_BANDS))
    qa = ee.Image.constant([1] * len(QA_BANDS)).rename(list(QA_BANDS))
    output = predict_month(train_rf(ee.FeatureCollection(rows)),
                           {"predictors": predictors, "qa": qa}, threshold=.5)
    graph = json.loads(output.serialize())
    outer = graph["values"][graph["result"]]["functionInvocationValue"]
    assert outer["functionName"] == "Image.addBands"
    selected = outer["arguments"]["srcImg"]["functionInvocationValue"]
    assert selected["functionName"] == "Image.select"
    selectors = selected["arguments"]["bandSelectors"]["arrayValue"]["values"]
    assert [graph["values"][selector["valueReference"]]["constantValue"] for selector in selectors] == ["s1_missing"]
