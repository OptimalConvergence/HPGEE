"""Bounded GEE fire-candidate detector. Labels and training eligibility are caller-owned.

The classifier contains only HLS, same-orbit Sentinel-1, and DEM features.
Product detections and active fires must never be passed as predictor bands.
"""
from __future__ import annotations

from datetime import date, timedelta

from geecomputation.features import COMMON6, hls_collection

HLS_COLLECTIONS = ("NASA/HLS/HLSL30/v002", "NASA/HLS/HLSS30/v002")
S1_COLLECTION = "COPERNICUS/S1_GRD"
DEM_COLLECTION = "COPERNICUS/DEM/GLO30_2024_1"
FEATURE_CONTRACT = "hls-s1-dem-monthpair-v1"
S1_FILL_DB = -99.0

HLS_FEATURES = (
    "pre_nir", "pre_swir1", "pre_swir2",
    "post_nir", "post_swir1", "post_swir2",
    "delta_nir", "delta_swir1", "delta_swir2",
    "pre_nbr", "pre_nbr2", "pre_mirbi",
    "post_nbr", "post_nbr2", "post_mirbi",
    "delta_nbr", "delta_nbr2", "delta_mirbi",
)
S1_FEATURES = (
    "s1_pre_vv_db", "s1_pre_vh_db", "s1_post_vv_db", "s1_post_vh_db",
    "s1_delta_vv_db", "s1_delta_vh_db", "s1_missing",
)
DEM_FEATURES = ("elevation", "slope", "aspect_sin", "aspect_cos")
FEATURE_BANDS = HLS_FEATURES + S1_FEATURES + DEM_FEATURES
QA_BANDS = (
    "hls_pre_count", "hls_post_count", "hls_pre_first_ms", "hls_pre_last_ms",
    "hls_post_first_ms", "hls_post_last_ms", "s1_pre_count", "s1_post_count",
)


def month_windows(year: int, month: int) -> dict[str, tuple[str, str]]:
    """UTC half-open previous/current/next calendar months."""
    if not (1 <= month <= 12 and 2013 <= year <= 2100):
        raise ValueError("year/month outside HLS analysis range")
    current = date(year, month, 1)
    previous = (current - timedelta(days=1)).replace(day=1)
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    after_next = date(next_month.year + (next_month.month == 12), next_month.month % 12 + 1, 1)
    fmt = lambda d: d.isoformat()
    return {
        "pre": (fmt(previous), fmt(current)),
        "post": (fmt(current), fmt(next_month)),
        "follow": (fmt(next_month), fmt(after_next)),
    }


def _masked_template(names):
    import ee
    return ee.Image.constant([0] * len(names)).rename(list(names)).toDouble().updateMask(ee.Image(0))


def _hls_month(region, start: str, end: str):
    import ee
    from types import SimpleNamespace
    # Native HLS tile support avoids oversized catalog geometries.
    cfg = SimpleNamespace(hls_tile_ids=(), hls_native_dimensions=(3660, 3660))
    source = hls_collection(cfg, geometry=region, start=start, end=end)
    masked = _masked_template(COMMON6)
    sr = ee.ImageCollection.fromImages([masked]).merge(source.select(list(COMMON6))).median()
    count = (ee.ImageCollection.fromImages([_masked_template(("nir",))])
             .merge(source.select("nir")).count().rename("count").unmask(0).toUint16())
    clocks = ee.ImageCollection.fromImages([_masked_template(("time_ms",))]).merge(source.select("time_ms"))
    first = clocks.min().rename("first_ms")
    last = clocks.max().rename("last_ms")
    return sr.select(list(COMMON6)), count, first, last


def _indices(sr, prefix):
    import ee
    nir, swir1, swir2 = (sr.select(b) for b in ("nir", "swir1", "swir2"))
    nbr_denom, nbr2_denom = nir.add(swir2), swir1.add(swir2)
    nbr = nir.subtract(swir2).divide(nbr_denom).updateMask(nbr_denom.abs().gt(1e-8))
    nbr2 = swir1.subtract(swir2).divide(nbr2_denom).updateMask(nbr2_denom.abs().gt(1e-8))
    mirbi = swir2.multiply(10).subtract(swir1.multiply(9.8)).add(2)
    return ee.Image.cat(nbr, nbr2, mirbi).rename([f"{prefix}_nbr", f"{prefix}_nbr2", f"{prefix}_mirbi"])


def _s1_month(region, start, end, orbit_pass, relative_orbit):
    import ee
    source = (ee.ImageCollection(S1_COLLECTION).filterBounds(region).filterDate(start, end)
              .filter(ee.Filter.eq("instrumentMode", "IW"))
              .filter(ee.Filter.eq("resolution_meters", 10))
              .filter(ee.Filter.eq("orbitProperties_pass", orbit_pass))
              .filter(ee.Filter.eq("relativeOrbitNumber_start", relative_orbit))
              .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
              .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
              .select(["VV", "VH"]))
    power = source.map(lambda image: ee.Image.constant(10).pow(ee.Image(image).divide(10))
                       .rename(["VV", "VH"]).toDouble())
    mean_power = ee.ImageCollection.fromImages([_masked_template(("VV", "VH"))]).merge(power).mean()
    db = mean_power.log10().multiply(10).rename(["vv_db", "vh_db"])
    count = (ee.ImageCollection.fromImages([_masked_template(("VV",))])
             .merge(source.select("VV")).count().rename("count").unmask(0).toUint16())
    return db, count


def feature_image(region, year: int, month: int, *, orbit_pass: str,
                  relative_orbit: int, grid_crs: str, grid_transform):
    """Return fused predictors and separate observational QA for one month pair.

    A fire anywhere within the target month may be detected in this or the next
    pair; the pair's month is not a burn-date assertion. HLS missing stays masked.
    """
    import ee
    if orbit_pass not in ("ASCENDING", "DESCENDING") or not (1 <= relative_orbit <= 175):
        raise ValueError("S1 requires one explicit pass and relative orbit")
    if not grid_crs or len(grid_transform) != 6:
        raise ValueError("explicit output CRS and affine are required")
    windows = month_windows(year, month)
    pre, pre_count, pre_first, pre_last = _hls_month(region, *windows["pre"])
    post, post_count, post_first, post_last = _hls_month(region, *windows["post"])
    pre_i, post_i = _indices(pre, "pre"), _indices(post, "post")
    sr_names = ("nir", "swir1", "swir2")
    optical = ee.Image.cat(
        pre.select(list(sr_names), [f"pre_{b}" for b in sr_names]),
        post.select(list(sr_names), [f"post_{b}" for b in sr_names]),
        post.select(list(sr_names)).subtract(pre.select(list(sr_names))).rename([f"delta_{b}" for b in sr_names]),
        pre_i, post_i,
        post_i.rename(["nbr", "nbr2", "mirbi"]).subtract(pre_i.rename(["nbr", "nbr2", "mirbi"]))
        .rename(["delta_nbr", "delta_nbr2", "delta_mirbi"]),
    ).select(list(HLS_FEATURES)).toFloat()
    s1_pre, s1_pre_count = _s1_month(region, *windows["pre"], orbit_pass, relative_orbit)
    s1_post, s1_post_count = _s1_month(region, *windows["post"], orbit_pass, relative_orbit)
    s1_missing = s1_pre_count.eq(0).Or(s1_post_count.eq(0)).rename("s1_missing").toFloat()
    s1 = ee.Image.cat(
        s1_pre.rename(["s1_pre_vv_db", "s1_pre_vh_db"]),
        s1_post.rename(["s1_post_vv_db", "s1_post_vh_db"]),
        s1_post.subtract(s1_pre).rename(["s1_delta_vv_db", "s1_delta_vh_db"]),
    ).unmask(S1_FILL_DB).addBands(s1_missing).select(list(S1_FEATURES)).toFloat()
    elevation = (ee.ImageCollection(DEM_COLLECTION).filterBounds(region).select("DEM").mosaic()
                 .reproject(crs=grid_crs, crsTransform=list(grid_transform)).rename("elevation"))
    terrain = ee.Terrain.products(elevation)
    aspect = terrain.select("aspect").multiply(3.141592653589793 / 180)
    dem = ee.Image.cat(elevation, terrain.select("slope"), aspect.sin(), aspect.cos()).rename(list(DEM_FEATURES)).toFloat()
    predictors = optical.addBands(s1).addBands(dem).select(list(FEATURE_BANDS))
    qa = ee.Image.cat(pre_count.rename("hls_pre_count"), post_count.rename("hls_post_count"),
                      pre_first.rename("hls_pre_first_ms"), pre_last.rename("hls_pre_last_ms"),
                      post_first.rename("hls_post_first_ms"), post_last.rename("hls_post_last_ms"),
                      s1_pre_count.rename("s1_pre_count"), s1_post_count.rename("s1_post_count")).select(list(QA_BANDS))
    return {"predictors": predictors, "qa": qa, "windows": windows,
            "feature_contract": FEATURE_CONTRACT, "bands": FEATURE_BANDS}


def followup_evidence(region, year: int, month: int):
    """HLS m+1 evidence for selected candidates; no persistence veto.

    A rapidly regreening burn may have weak follow-up contrast. Call this on
    a bounded candidate ROI and retain missing observations as unknown.
    """
    import ee
    window = month_windows(year, month)["follow"]
    sr, count, first, last = _hls_month(region, *window)
    nbr = _indices(sr, "follow").select("follow_nbr")
    return ee.Image.cat(nbr, count.rename("hls_follow_count"),
                        first.rename("hls_follow_first_ms"),
                        last.rename("hls_follow_last_ms"))


def train_rf(training_features, *, seed: int = 20261019, trees: int = 100):
    """Build a lazy EE classifier; caller must freeze and qualify the table."""
    import ee
    if trees != 100:
        raise ValueError("v1 comparison freezes 100 trees")
    return (ee.Classifier.smileRandomForest(numberOfTrees=trees, seed=seed)
            .setOutputMode("MULTIPROBABILITY")
            .train(ee.FeatureCollection(training_features), "label", list(FEATURE_BANDS)))


def predict_month(model, feature_product, *, threshold: float):
    import ee
    if not 0 < threshold < 1:
        raise ValueError("threshold must be frozen on non-BARD development data")
    score = feature_product["predictors"].classify(model).arrayFlatten([["score_unburned", "score_burned"]])
    burned = score.select("score_burned")
    candidate = burned.gte(threshold).rename("candidate").toUint8()
    return ee.Image.cat(score, candidate, feature_product["qa"],
                        feature_product["predictors"].select("s1_missing"))
