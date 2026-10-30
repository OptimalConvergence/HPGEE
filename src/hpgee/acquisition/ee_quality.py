"""Batch per-cell source support and cloud statistics; immutable local caches."""

import json
import time
from pathlib import Path
from .quality import POLICY, cache_key, compare_decisions


def affine(grid):
    a = grid["affineTransform"]
    return [
        a["scaleX"],
        a.get("shearX", 0),
        a["translateX"],
        a.get("shearY", 0),
        a["scaleY"],
        a["translateY"],
    ]


def cell_grid(grid, row, col, height=128, width=128):
    result = json.loads(json.dumps(grid))
    a = result["affineTransform"]
    x = col * 128
    y = row * 128
    a["translateX"] += x * a["scaleX"] + y * a.get("shearX", 0)
    a["translateY"] += x * a.get("shearY", 0) + y * a["scaleY"]
    result["dimensions"] = dict(width=width, height=height)
    return result


def geometry(ee, grid):
    a = affine(grid)
    w, h = (grid["dimensions"][k] for k in ("width", "height"))
    corners = [
        [a[0] * x + a[1] * y + a[2], a[3] * x + a[4] * y + a[5]]
        for x, y in ((0, 0), (w, 0), (w, h), (0, h), (0, 0))
    ]
    return ee.Geometry.Polygon([corners], proj=grid["crsCode"], geodesic=False)


def cells(grid):
    width, height = (grid["dimensions"][k] for k in ("width", "height"))
    return [
        dict(row=y // 128, col=x // 128, height=min(128, height - y), width=min(128, width - x))
        for y in range(0, height, 128)
        for x in range(0, width, 128)
    ]


def domain_mask(ee, task):
    if task.get("land_asset"):
        land = (
            ee.Image(0)
            .byte()
            .paint(
                ee.FeatureCollection(task["land_asset"]).filterBounds(geometry(ee, task["grid"])), 1
            )
        )
    else:
        land = ee.Image(1)
    if task.get("owner_geometry"):
        land = land.clip(ee.Geometry(task["owner_geometry"])).unmask(0, False)
    # Fix rasterization to one native grid before consumers request other tiles.
    grid = task["grid"]
    return land.reproject(grid["crsCode"], affine(grid))


def mask_image(ee, task, scale):
    from hpgee.planning.observations import HLS_BANDS

    sensor = task["sensor"]
    grid = task["grid"]
    parts = []
    for source in task["sources"]:
        image = ee.Image(source["scene"]["asset_id"])
        if sensor in ("S2", "DW"):
            from .recipes import recipe

            r = recipe(sensor)
            bands = r["bands"][:-1]
            image = image.select(bands)
            common = image.mask().reduce(ee.Reducer.min())
            valid = image.select(bands[0]).multiply(0).add(1).updateMask(common)
            cloud = (
                image.select("SCL").gte(8).And(image.select("SCL").lte(10))
                if sensor == "S2"
                else valid.multiply(0)
            )
            part = (
                valid.rename("valid")
                .addBands(cloud.rename("cloud"))
                .addBands(valid.multiply(0).rename("shadow"))
                .updateMask(common)
            )
        elif sensor == "S1":
            vv, vh = image.select("VV"), image.select("VH")
            common = vv.mask().And(vh.mask())
            valid = vv.multiply(0).add(1).updateMask(common)
            part = (
                valid.rename("valid")
                .addBands(valid.multiply(0).rename("cloud"))
                .addBands(valid.multiply(0).rename("shadow"))
            )
        else:
            spectral = image.select(list(HLS_BANDS[sensor]))
            qa = image.select("Fmask")
            common = spectral.mask().reduce(ee.Reducer.min()).And(qa.mask())
            valid = qa.multiply(0).add(1).updateMask(common)
            part = (
                valid.rename("valid")
                .addBands(qa.bitwiseAnd(2).neq(0).rename("cloud"))
                .addBands(qa.bitwiseAnd(8).neq(0).rename("shadow"))
                .updateMask(common)
            )
        parts.append(part)
    # Mosaic only copies of one observation, preserving each source's joint mask.
    result = (
        ee.ImageCollection.fromImages(parts)
        .mosaic()
        .setDefaultProjection(grid["crsCode"], affine(grid))
    )
    result = result.unmask(0, False)
    if sensor == "S1" and scale == 30:
        # The production 30m power recipe accepts any positive native support.
        # Preserve a native input projection for reduceResolution, not the 30m default.
        native = (
            ee.ImageCollection.fromImages(parts)
            .mosaic()
            .setDefaultProjection(parts[0].select("valid").projection())
        )
        supported = (
            native.select("valid")
            .unmask(0, False)
            .reduceResolution(ee.Reducer.sum(), maxPixels=1024, bestEffort=False)
            .reproject(grid["crsCode"], affine(grid))
            .gt(0)
        )
        result = (
            supported.rename("valid")
            .addBands(supported.multiply(0).rename("cloud"))
            .addBands(supported.multiply(0).rename("shadow"))
        )
    land = domain_mask(ee, task)
    # No averaging of categorical Fmask integers; each statistic is boolean first.
    result = result.multiply(land).addBands(land.rename("land")).unmask(0, False)
    return result


def collect(ee, task, scale, cache_dir):
    ownership = dict(owner_geometry=task["owner_geometry"]) if task.get("owner_geometry") else {}
    key = cache_key(
        sources=task["sources"],
        grid=task["grid"],
        sensor=task["sensor"],
        scale=scale,
        land_asset=task.get("land_asset"),
        observation=task["observation"],
        count_engine="fixed-land-grid-v3",
        **ownership,
    )
    path = Path(cache_dir) / (key + ".json")
    if path.exists():
        data = json.loads(path.read_text())
        if data["key"] != key:
            raise ValueError("Quality cache identity mismatch")
        return dict(data, cache_hit=True, read_seconds=0)
    from .counts import count_grid, rows_from_array

    began = time.monotonic()
    grid = task["grid"]
    transform = affine(grid)
    transform[0] = scale
    transform[4] = -scale
    # Categorical flags have already been decoded into booleans. Aligned area
    # fractions multiplied by 128**2 recover native 30m pixel-equivalent counts.
    image = mask_image(ee, task, scale).toDouble().clip(geometry(ee, grid)).unmask(0, False)
    image = image.reproject(grid["crsCode"], transform)
    counts = (
        image.reduceResolution(ee.Reducer.sum(), maxPixels=65536, bestEffort=False)
        .reproject(grid["crsCode"], affine(count_grid(grid)))
        .multiply(128**2)
        .round()
        .toInt32()
    )
    request = dict(
        expression=counts,
        grid=count_grid(grid),
        bandIds=["valid", "cloud", "shadow", "land"],
        fileFormat="NUMPY_NDARRAY",
    )
    if task.get("workload_tag"):
        request["workloadTag"] = task["workload_tag"]
    from hpgee.runtime.retry import retry_read

    array = retry_read(
        lambda: ee.data.computePixels(request), attempts=2, base_seconds=1, cap_seconds=1
    )
    rows = rows_from_array(array, grid, task["sensor"], scale)
    data = dict(
        key=key,
        scale=scale,
        seconds=time.monotonic() - began,
        rows=rows,
        policy=POLICY,
        cache_hit=False,
        scope="exact native counts"
        if scale == abs(affine(grid)[0])
        else "coarse approximate counts",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)
    return data


def audit(ee, task, cache_dir):
    exact = collect(ee, task, 30, cache_dir)
    reports = {}
    for scale in (100, 200):
        approximate = collect(ee, task, scale, cache_dir)
        reports[str(scale)] = dict(
            compare_decisions(exact["rows"], approximate["rows"]),
            seconds=approximate["seconds"],
            cache_hit=approximate["cache_hit"],
        )
    # Selection remains the same exact manifest during transport comparisons.
    return dict(exact=exact, approximations=reports, selection="frozen exact 30m decisions")
