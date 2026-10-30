import numpy as np
from hpgee.acquisition.counts import count_grid, rows_from_array


def test_count_grid_and_partial_edges_are_small_and_aligned():
    grid = {
        "crsCode": "EPSG:32648",
        "dimensions": {"width": 256, "height": 129},
        "affineTransform": {"scaleX": 30, "scaleY": -30, "translateX": 100, "translateY": 200},
    }
    result = count_grid(grid)
    assert result["dimensions"] == {"width": 2, "height": 2}
    assert result["affineTransform"]["scaleX"] == 3840
    assert result["affineTransform"]["translateX"] == 100
    array = np.zeros((2, 2), dtype=[(n, "i4") for n in ("valid", "cloud", "shadow", "land")])
    array["valid"][:] = 16384
    array["land"][:] = 16384
    rows = rows_from_array(array, grid, "S30", 30)
    assert sum(r["total"] for r in rows) == 256 * 129
    assert rows[-1]["valid"] == 128 and rows[-1]["height"] == 1
    assert all(r["keep"] for r in rows)
