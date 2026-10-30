"""Aligned coarse count rasters avoid returning hundreds of feature geometries."""

import json
from .quality import decide


def count_grid(grid):
    out = json.loads(json.dumps(grid))
    a = out["affineTransform"]
    out["dimensions"] = {k: (grid["dimensions"][k] + 127) // 128 for k in ("width", "height")}
    a["scaleX"] *= 128
    a["scaleY"] *= 128
    for name in ("shearX", "shearY"):
        if name in a:
            a[name] *= 128
    return out


def rows_from_array(array, grid, sensor, scale):
    height, width = (grid["dimensions"][k] for k in ("height", "width"))
    expected = ((height + 127) // 128, (width + 127) // 128)
    if array.shape != expected or not {"valid", "cloud", "shadow", "land"} <= set(
        array.dtype.names or ()
    ):
        raise ValueError("Count raster shape/bands differ from target")
    rows = []
    for y in range(expected[0]):
        for x in range(expected[1]):
            h, w = min(128, height - y * 128), min(128, width - x * 128)
            n = h * w
            valid = min(n, max(0, int(round(float(array["valid"][y, x])))))
            cloud = min(valid, max(0, int(round(float(array["cloud"][y, x])))))
            shadow = min(valid, max(0, int(round(float(array["shadow"][y, x])))))
            land = min(n, max(0, int(round(float(array["land"][y, x])))))
            rows.append(
                dict(
                    row=y,
                    col=x,
                    height=h,
                    width=w,
                    total=n,
                    valid=valid,
                    cloud=cloud,
                    shadow=shadow,
                    land=land,
                    scale=scale,
                    **decide(valid, cloud, n, sensor),
                )
            )
    return rows
