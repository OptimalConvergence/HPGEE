"""Aligned coarse count rasters avoid returning hundreds of feature geometries."""

import json
from .quality import decide


def count_grid(grid, *, cell_pixels=128):
    out = json.loads(json.dumps(grid))
    a = out["affineTransform"]
    out["dimensions"] = {k: (grid["dimensions"][k] + (cell_pixels - 1)) // cell_pixels for k in ("width", "height")}
    a["scaleX"] *= cell_pixels
    a["scaleY"] *= cell_pixels
    for name in ("shearX", "shearY"):
        if name in a:
            a[name] *= cell_pixels
    return out


def rows_from_array(array, grid, sensor, scale, *, cell_pixels=128):
    height, width = (grid["dimensions"][k] for k in ("height", "width"))
    expected = ((height + (cell_pixels - 1)) // cell_pixels, (width + (cell_pixels - 1)) // cell_pixels)
    if array.shape != expected or not {"valid", "cloud", "shadow", "land"} <= set(
        array.dtype.names or ()
    ):
        raise ValueError("Count raster shape/bands differ from target")
    rows = []
    for y in range(expected[0]):
        for x in range(expected[1]):
            h, w = min(cell_pixels, height - y * cell_pixels), min(cell_pixels, width - x * cell_pixels)
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
