import numpy as np
import pytest


def task():
    return dict(
        region="r",
        observation="o",
        sensor="S30",
        land_asset="LAND",
        sources=[{"scene": {"asset_id": "NASA/HLS/HLSS30/v002/test"}}],
        grid={
            "crsCode": "EPSG:32648",
            "dimensions": {"width": 128, "height": 128},
            "affineTransform": {
                "scaleX": 30,
                "scaleY": -30,
                "translateX": 500000,
                "translateY": 3000000,
            },
        },
    )


def test_resume_identity_changes_when_mask_or_recipe_changes():
    from hpgee.acquisition.manifest import freeze_task

    base = task()
    quality = dict(key="q", rows=[])
    original = freeze_task(base, quality)["id"]
    assert original != freeze_task(dict(base, land_asset="OTHER"), quality)["id"]
    assert original != freeze_task(dict(base, recipe_version="changed"), quality)["id"]


def test_post_quality_freeze_needs_no_remote_pixel_statistics():
    from hpgee.acquisition.manifest import freeze_unfiltered, requests_for_task

    target = freeze_unfiltered(task())
    assert len(target["cells"]) == 1 and target["selection"] == "post"
    assert target["cells"][0]["valid"] is None
    assert len(requests_for_task(target, 512)) == 1


def test_unknown_frozen_counts_are_measured_from_actual_payload():
    from hpgee.acquisition.manifest import freeze_unfiltered, requests_for_task
    from hpgee.acquisition.delivery import validate_array

    request = requests_for_task(freeze_unfiltered(task()), 512)[0]
    values = np.zeros((8, 128, 128), dtype="int16")
    values[7, :64] = 127
    result = validate_array(values, request)
    assert result["valid_pixels"] == 8192 and result["cloud_pixels"] == 0
    assert result["quality_kept_cells"] == 1
    values[6, :64] = 2
    result = validate_array(values, request)
    assert result["quality_kept_cells"] == 0 and result["quality_rejected_cells"] == 1


def test_dynamic_world_keeps_water_label_zero_and_probabilities():
    from hpgee.acquisition.manifest import freeze_unfiltered, requests_for_task
    from hpgee.acquisition.delivery import validate_array

    t = task()
    t["sensor"] = "DW"
    t["land_asset"] = None
    request = requests_for_task(freeze_unfiltered(t), 512)[0]
    values = np.zeros((11, 128, 128), dtype="float32")
    values[0] = 1
    values[-1] = 1
    result = validate_array(values, request)
    assert result["valid_pixels"] == 16384
    values[9] = 0.25
    with pytest.raises(ValueError, match="categorical"):
        validate_array(values, request)


def test_s2_does_not_require_optional_cloud_probability_bands():
    from hpgee.acquisition.recipes import recipe

    r = recipe("S2")
    assert "B12" in r["bands"] and "SCL" in r["bands"]
    assert len(r["spectral"]) == 12 and r["dtype"] == "uint16"
