import numpy as np
import pytest
from hpgee.acquisition.delivery import validate_array


def request(sensor):
    return dict(
        sensor=sensor,
        grid={"dimensions": {"width": 128, "height": 128}},
        row=0,
        col=0,
        cells=[dict(id="fixed-cell", row=0, col=0, width=128, height=128, valid=128**2, cloud=0)],
    )


def test_full_hls_accepts_the_frozen_cell_and_pixel_counts():
    data = np.zeros((8, 128, 128), dtype="int16")
    data[7] = 127
    a = validate_array(data, request("S30"))
    assert a["cells"] == ["fixed-cell"]
    assert a["valid_pixels"] == 128**2


def test_false_success_with_missing_expected_pixels_is_refused():
    data = np.zeros((8, 128, 128), dtype="int16")
    with pytest.raises(ValueError):
        validate_array(data, request("L30"))


def test_overflow_flag_cannot_be_published_as_lossless_hls():
    data = np.zeros((8, 128, 128), dtype="int16")
    data[0] = 32767
    data[7] = 255
    with pytest.raises(ValueError, match="overflow"):
        validate_array(data, request("L30"))


def test_s1_keeps_zero_power_and_partial_support_fraction():
    data = np.zeros((4, 128, 128), dtype="float32")
    data[2] = 3
    data[3] = 0.25
    a = validate_array(data, request("S1"))
    assert a["valid_pixels"] == 128**2


def tiff_bytes(values, grid):
    from rasterio.io import MemoryFile
    from rasterio import Affine
    from hpgee.acquisition.ee_quality import affine

    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            width=values.shape[2],
            height=values.shape[1],
            count=len(values),
            dtype=values.dtype,
            crs=grid["crsCode"],
            transform=Affine(*affine(grid)),
        ) as ds:
            ds.write(values)
        return mem.read()


def test_publication_checks_the_payload_grid_itself(tmp_path):
    from hpgee.acquisition.delivery import publish, verified

    req = request("S30")
    req["id"] = "fixed-request"
    req["grid"].update(
        crsCode="EPSG:32648",
        affineTransform={"scaleX": 30, "scaleY": -30, "translateX": 500000, "translateY": 3000000},
    )
    data = np.zeros((8, 128, 128), dtype="int16")
    data[7] = 127
    payload = tiff_bytes(data, req["grid"])
    receipt = publish(payload, req, tmp_path / "valid")
    assert receipt["valid_pixels"] == 128**2 and verified(req, tmp_path / "valid")
    wrong = dict(
        req["grid"], affineTransform=dict(req["grid"]["affineTransform"], translateX=500030)
    )
    with pytest.raises(ValueError, match="grid"):
        publish(tiff_bytes(data, wrong), req, tmp_path / "wrong")
    assert not (tmp_path / "wrong").exists()
