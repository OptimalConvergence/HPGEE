import json
from types import SimpleNamespace
import pytest
from hpgee.acquisition import images


def test_nonfinite_image_constant_is_rejected_before_sending_invalid_json(monkeypatch):
    monkeypatch.setattr(images, "selected_image", lambda *a: object())
    ee = SimpleNamespace(
        serializer=SimpleNamespace(encode=lambda image: {"constantValue": float("nan")})
    )
    with pytest.raises(ValueError, match="JSON"):
        images.request_body(ee, {"sensor": "S1"}, {"grid": {}}, "tag")


def test_finite_zero_is_valid_and_does_not_change_s1_band_contract(monkeypatch):
    monkeypatch.setattr(images, "selected_image", lambda *a: object())
    ee = SimpleNamespace(serializer=SimpleNamespace(encode=lambda image: {"constantValue": 0}))
    body = json.loads(images.request_body(ee, {"sensor": "S1"}, {"grid": {}}, "tag"))
    assert body["bandIds"] == ["VV_power", "VH_power", "source_valid_bits", "valid_fraction"]
    assert body["expression"]["constantValue"] == 0
