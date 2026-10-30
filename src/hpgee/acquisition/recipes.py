"""Explicit lossless output contracts; scientific values are independent of routes."""

import copy
from hpgee.transfer.raw_download import HLS_INT16_RECIPE, S1_30_RECIPE

HLS = ["blue", "green", "red", "nir", "swir1", "swir2", "Fmask", "source_valid_bits"]
S2 = ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B11", "B12"]
DW = [
    "water",
    "trees",
    "grass",
    "flooded_vegetation",
    "crops",
    "shrub_and_scrub",
    "built",
    "bare",
    "snow_and_ice",
]
RECIPES = {
    "L30": dict(
        collection="NASA/HLS/HLSL30/v002",
        bands=HLS,
        dtype="int16",
        version=HLS_INT16_RECIPE,
        reference="B2",
        scale=30,
    ),
    "S30": dict(
        collection="NASA/HLS/HLSS30/v002",
        bands=HLS,
        dtype="int16",
        version=HLS_INT16_RECIPE,
        reference="B2",
        scale=30,
    ),
    "S1": dict(
        collection="COPERNICUS/S1_GRD",
        bands=["VV_power", "VH_power", "source_valid_bits", "valid_fraction"],
        dtype="float32",
        version=S1_30_RECIPE,
        reference="VV",
        scale=30,
    ),
    "S2": dict(
        collection="COPERNICUS/S2_SR_HARMONIZED",
        spectral=S2,
        bands=S2 + ["SCL", "source_valid_bits"],
        dtype="uint16",
        version="s2-sr-12bands-scl-nearest-v1",
        reference="B2",
        scale=10,
    ),
    "DW": dict(
        collection="GOOGLE/DYNAMICWORLD/V1",
        bands=DW + ["label", "source_valid_bits"],
        dtype="float32",
        version="dw-9prob-label-nearest-v1",
        reference="label",
        scale=10,
    ),
}


def recipe(sensor):
    try:
        return copy.deepcopy(RECIPES[sensor])
    except KeyError:
        raise ValueError("Unknown dataset recipe") from None


def generic_image(ee, task):
    """S2 mixed-resolution bands and DW classes use nearest-neighbour sampling.

    S2 retains scaled integer SR, SCL and per-band support. DW Float32 preserves
    probability values and integer labels including valid water(label0).
    """
    sensor = task["sensor"]
    r = recipe(sensor)
    parts = []
    names = r["bands"][:-1]
    for source in task["sources"]:
        image = ee.Image(source["scene"]["asset_id"]).select(names)
        masks = [image.select(name).mask().unmask(0, False) for name in names]
        if sensor == "S2":
            bits = masks[0]
            for i, mask in enumerate(masks[1:], 1):
                bits = bits.add(mask.multiply(1 << i))
            part = image.addBands(bits.rename("source_valid_bits")).toUint16()
            common = bits.eq((1 << len(names)) - 1)
        else:
            common = masks[0]
            for mask in masks[1:]:
                common = common.And(mask)
            part = image.addBands(common.rename("source_valid_bits")).toFloat()
        parts.append(part.updateMask(common))
    return ee.ImageCollection.fromImages(parts).mosaic()
