"""Offline tests for the staged raw HLS Int16 profile candidate."""
from __future__ import annotations

import sys
from pathlib import Path
import types
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
try:
    import rasterio
    from affine import Affine
except ModuleNotFoundError:
    rasterio = None
    Affine = None

CANDIDATE = Path(__file__).resolve().parents[2] / "candidate"
if (CANDIDATE / "geecomputation").is_dir():
    sys.path.insert(0, str(CANDIDATE))
    observation_stub = types.ModuleType("geecomputation.observations")
    observation_stub.observation_image = lambda *args, **kwargs: None
    observation_stub.S1_COLLECTION = "COPERNICUS/S1_GRD"
    sys.modules.setdefault("geecomputation.observations", observation_stub)
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from geecomputation import raw_delivery, raw_download  # noqa: E402


def grid(width=2, height=2):
    return {
        "crsCode": "EPSG:32610",
        "affineTransform": {
            "scaleX": 30, "shearX": 0, "translateX": 499980,
            "shearY": 0, "scaleY": -30, "translateY": 4500000,
        },
        "dimensions": {"width": width, "height": height},
    }


def scene(collection="HLS/TEST", asset="HLS/TEST/example"):
    return {"collection": collection, "asset_id": asset, "time_ms": 0}


def write_payload(path, values, dtype):
    if rasterio is None:
        raise RuntimeError("Rasterio is required for synthetic publisher integration tests")
    with rasterio.open(
        path, "w", driver="GTiff", width=2, height=2, count=8,
        dtype=dtype, crs="EPSG:32610",
        transform=Affine(30, 0, 499980, 0, -30, 4500000),
    ) as dataset:
        dataset.write(values)


class FakeImage:
    def __init__(self, events=None):
        self.events = events if events is not None else []

    def _record(self, name, *args):
        self.events.append((name, *args))
        return FakeImage(self.events)

    def select(self, bands):
        return self._record("select", tuple(bands) if isinstance(bands, (list, tuple)) else bands)

    def multiply(self, value): return self._record("multiply", value)
    def round(self): return self._record("round")
    def lt(self, value): return self._record("lt", value)
    def gt(self, value): return self._record("gt", value)
    def Or(self, other): return self._record("Or")
    def reduce(self, reducer): return self._record("reduce", reducer)
    def unmask(self, value, same_footprint): return self._record("unmask", value, same_footprint)
    def add(self, other): return self._record("add")
    def addBands(self, other): return self._record("addBands")
    def toInt16(self): return self._record("toInt16")
    def set(self, properties): return self._record("set", tuple(sorted(properties.items())))


def test_int16_profile_has_a_separate_recipe_and_dtype_aware_identity():
    source = scene()
    legacy = raw_download.make_unit([source], grid(), "hls")
    encoded = raw_download.make_unit([source], grid(), "hls-int16")

    assert legacy["recipe"] == raw_download.RECIPE
    assert legacy["logical_bytes"] == 2 * 2 * 8 * 4
    assert set(legacy) == {"sources", "grid", "profile", "recipe", "unit_id", "scenes", "bands", "logical_bytes"}
    assert encoded["profile"] == "hls-int16"
    assert encoded["recipe"] == raw_download.HLS_INT16_RECIPE
    assert encoded["encoding"] == {
        "dtype": "int16", "scale": 0.0001, "offset": 0.0,
        "qa_bands": ["Fmask", "source_valid_bits"],
        "overflow_flag_bit": 7,
    }
    assert encoded["unit_id"] != legacy["unit_id"]
    assert encoded["logical_bytes"] == 2 * 2 * 8 * 2
    assert encoded["bands"] == [f"t0_{name}" for name in raw_download.HLS_BANDS]
    assert raw_download.pack_limit(1024, 1024, 8, 2) == 1
    assert raw_download.pack_limit(1024, 1024, 8, 2, itemsize=2) == 2


def test_int16_image_graph_scales_reflectance_and_sets_overflow_flag():
    events = []
    fake_ee = SimpleNamespace(Reducer=SimpleNamespace(max=lambda: "max"))
    with patch.object(raw_download, "observation_image", return_value=FakeImage(events)), \
            patch.dict(sys.modules, {"ee": fake_ee}):
        image = raw_download.raw_image(scene(), grid(), "hls-int16")

    assert isinstance(image, FakeImage)
    assert ("multiply", 10000) in events
    assert ("round",) in events
    assert ("lt", -32768) in events
    assert ("gt", 32767) in events
    assert ("Or",) in events
    assert ("reduce", "max") in events
    assert ("unmask", 0, False) in events
    assert events.count(("toInt16",)) >= 3
    assert ("set", tuple(sorted({
        "observation_recipe": raw_download.HLS_INT16_RECIPE,
        "encoding_scale": 0.0001,
        "encoding_offset": 0.0,
        "overflow_flag_bit": 7,
    }.items()))) in events


def test_int16_identity_mismatch_fails_before_payload_or_ee_access(tmp_path):
    unit = raw_download.make_unit([scene()], grid(), "hls-int16")
    unit["encoding"]["scale"] = 0.001
    try:
        raw_delivery.publish(unit, tmp_path / "does-not-exist.tif", tmp_path / "accepted")
    except ValueError as error:
        assert "identity mismatch" in str(error)
    else:
        raise AssertionError("Publisher must reject mutated scale/identity")

    unit = raw_download.make_unit([scene()], grid(), "hls-int16")
    unit["recipe"] = "wrong-recipe"
    try:
        raw_download.unit_image(unit)
    except ValueError as error:
        assert "identity mismatch" in str(error)
    else:
        raise AssertionError("EE image construction must reject mutated recipe")


def test_int16_publisher_preserves_codes_scale_and_companion_qa(tmp_path):
    unit = raw_download.make_unit([scene()], grid(), "hls-int16")
    values = np.zeros((8, 2, 2), dtype="int16")
    values[:6, :, :] = np.array([-200, 0, 1234, 255], dtype="int16").reshape(1, 2, 2)
    values[6] = np.array([[3, 7], [255, 9]], dtype="int16")
    values[7] = np.array([[127, 127], [63, 0]], dtype="int16")
    payload = tmp_path / "payload-int16.tif"
    write_payload(payload, values, "int16")

    rows = raw_delivery.publish(unit, payload, tmp_path / "accepted")
    assert len(rows) == 1
    receipt = rows[0]
    assert receipt["identity"] == raw_delivery.atoms(unit)[0]
    assert receipt["encoding"] == unit["encoding"]
    assert receipt["logical_transport_bytes"] == 2 * 2 * 8 * 2
    assert raw_delivery.verified(unit, tmp_path / "accepted")

    with rasterio.open(receipt["data_path"]) as dataset:
        assert dataset.dtypes == ("int16",) * 6
        assert dataset.scales == (0.0001,) * 6
        assert dataset.offsets == (0.0,) * 6
        assert dataset.tags()["encoding"] == raw_download.HLS_INT16_RECIPE
        data = dataset.read()
        assert int(data[0, 0, 0]) == -200
        assert int(data[0, 0, 1]) == 0  # valid zero reflectance remains a zero code
        assert int(data[0, 1, 1]) == 0  # invalid fill is also zero, distinguished by QA
        assert dataset.nodata is None
    with rasterio.open(receipt["qa_path"]) as dataset:
        qa = dataset.read()
        assert dataset.dtypes == ("uint8", "uint8")
        assert int(qa[0, 0, 0]) == 3
        assert int(qa[0, 0, 1]) == 7
        assert int(qa[0, 1, 0]) == 0  # Fmask is absent where bit 6 is clear
        assert np.array_equal(qa[1], np.array([[127, 127], [63, 0]], dtype="uint8"))


def test_publisher_rejects_marked_int16_overflow_before_writing_atoms(tmp_path):
    unit = raw_download.make_unit([scene()], grid(), "hls-int16")
    values = np.zeros((8, 2, 2), dtype="int16")
    values[7, 0, 0] = 128
    payload = tmp_path / "overflow.tif"
    write_payload(payload, values, "int16")
    destination = tmp_path / "accepted"

    try:
        raw_delivery.publish(unit, payload, destination)
    except ValueError as error:
        assert "overflow" in str(error).lower()
    else:
        raise AssertionError("Int16 overflow sentinel must reject the payload")
    assert not list(destination.glob("*.json"))


def test_legacy_hls_and_s1_profiles_keep_float32_defaults(tmp_path):
    hls = raw_download.make_unit([scene()], grid(), "hls")
    s1 = raw_download.make_unit(
        [scene("COPERNICUS/S1_GRD", "COPERNICUS/S1_GRD/example")], grid(), "s1-10")
    assert hls["recipe"] == raw_download.RECIPE
    assert "encoding" not in hls
    assert hls["logical_bytes"] == 2 * 2 * 8 * 4
    assert s1["recipe"] == raw_download.RECIPE
    assert "encoding" not in s1
    assert s1["logical_bytes"] == 2 * 2 * 4 * 4

    payload = np.ones((8, 2, 2), dtype="float32")
    payload[6] = 2
    payload[7] = 127
    source = tmp_path / "legacy-float32.tif"
    write_payload(source, payload, "float32")
    receipt = raw_delivery.publish(hls, source, tmp_path / "legacy-accepted")[0]
    with rasterio.open(receipt["data_path"]) as dataset:
        assert dataset.dtypes == ("float32",) * 6
        assert dataset.scales == (1.0,) * 6


class CandidateContractTests(unittest.TestCase):
    def test_profile_identity_and_dtype_budget(self):
        test_int16_profile_has_a_separate_recipe_and_dtype_aware_identity()

    def test_image_encoding_graph(self):
        test_int16_image_graph_scales_reflectance_and_sets_overflow_flag()

    def test_identity_mismatch_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_int16_identity_mismatch_fails_before_payload_or_ee_access(Path(tmp))

    def test_int16_publish_and_qa(self):
        if rasterio is None:
            self.skipTest("Rasterio is unavailable in the selected interpreter")
        with tempfile.TemporaryDirectory() as tmp:
            test_int16_publisher_preserves_codes_scale_and_companion_qa(Path(tmp))

    def test_overflow_rejection(self):
        if rasterio is None:
            self.skipTest("Rasterio is unavailable in the selected interpreter")
        with tempfile.TemporaryDirectory() as tmp:
            test_publisher_rejects_marked_int16_overflow_before_writing_atoms(Path(tmp))

    def test_legacy_defaults(self):
        if rasterio is None:
            self.skipTest("Rasterio is unavailable in the selected interpreter")
        with tempfile.TemporaryDirectory() as tmp:
            test_legacy_hls_and_s1_profiles_keep_float32_defaults(Path(tmp))
