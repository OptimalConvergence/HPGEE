"""Scientific images and selected-cell masks; transport packing changes no targets."""

from .ee_quality import affine, geometry, cell_grid, domain_mask
from .quality import cell_pixels


def s1_mosaic_image(ee, sources, grid):
    import math

    parts = []
    for source in sources:
        image = ee.Image(source["scene"]["asset_id"]).select(["VV", "VH"])
        common = image.select("VV").mask().And(image.select("VH").mask())
        parts.append(image.updateMask(common))
    source = (
        ee.ImageCollection.fromImages(parts)
        .mosaic()
        .setDefaultProjection(parts[0].select("VV").projection())
    )
    vv, vh = source.select("VV"), source.select("VH")
    common_mask = vv.mask().And(vh.mask())
    support = vv.multiply(0).add(1).updateMask(common_mask).unmask(0, False)

    def overlap(value):
        return value.reduceResolution(ee.Reducer.sum(), maxPixels=1024, bestEffort=False).reproject(
            grid["crsCode"], affine(grid)
        )

    fraction = overlap(support).rename("valid_fraction")
    powers = []
    for band, name in ((vv, "VV_power"), (vh, "VH_power")):
        power = (
            band.divide(10).multiply(math.log(10)).exp().updateMask(common_mask).unmask(0, False)
        )
        powers.append(overlap(power).divide(fraction).updateMask(fraction.gt(0)).rename(name))
    return (
        powers[0]
        .addBands(powers[1])
        .addBands(fraction.gt(0).multiply(3).rename("source_valid_bits"))
        .addBands(fraction)
        .toFloat()
    )


def full_image(ee, task):
    from hpgee.transfer.raw_download import raw_image

    sensor = task["sensor"]
    grid = task["grid"]
    if sensor in ("S2", "DW"):
        from .recipes import generic_image

        return generic_image(ee, task)
    if sensor == "S1":
        image = (
            raw_image(task["sources"][0]["scene"], grid, "s1-30")
            if len(task["sources"]) == 1
            else s1_mosaic_image(ee, task["sources"], grid)
        )
    else:
        fallback = []
        complete = []
        for source in task["sources"]:
            raw = raw_image(source["scene"], grid, "hls-int16")
            bits = raw.select("source_valid_bits")
            raw = raw.unmask(0, False)
            fallback.append(raw.updateMask(bits.bitwiseAnd(127).neq(0)))
            complete.append(raw.updateMask(bits.bitwiseAnd(127).eq(127)))
        fallback = ee.ImageCollection.fromImages(fallback).mosaic()
        complete = ee.ImageCollection.fromImages(complete).mosaic()
        image = fallback.blend(complete).setDefaultProjection(grid["crsCode"], affine(grid))
    return image


def selected_image(ee, task, request):
    image = full_image(ee, task)
    features = [
        ee.Feature(
            geometry(
                ee, cell_grid(task["grid"], cell["row"], cell["col"], cell["height"], cell["width"],
                              cell_pixels=cell_pixels(task))
            )
        )
        for cell in request["cells"]
    ]
    selected = ee.Image(0).byte().paint(ee.FeatureCollection(features), 1)
    selected = selected.And(domain_mask(ee, task))
    image = image.updateMask(selected).unmask(0, False)
    if task["sensor"] == "S2":
        return image.toUint16()
    if task["sensor"] in ("S1", "DW"):
        return image.toFloat()
    return image.toInt16()


def output_bands(sensor):
    from .recipes import recipe

    return recipe(sensor)["bands"]


def request_body(ee, task, request, tag):
    import json

    image = selected_image(ee, task, request)
    return json.dumps(
        dict(
            expression=ee.serializer.encode(image),
            grid=request["grid"],
            bandIds=output_bands(task["sensor"]),
            fileFormat="GEO_TIFF",
            workloadTag=tag,
        ),
        separators=(",", ":"),
        allow_nan=False,
    )
