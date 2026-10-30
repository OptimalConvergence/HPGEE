"""Pure numerical quality policy; cloud and source absence are different states."""

import hashlib
import json
import numpy as np

POLICY = dict(
    cell_pixels=128,
    min_supported=0.15,
    max_cloud=0.80,
    cloud_bits=2,
    required_hls_source_bits=127,
    s1_support="valid_fraction>0",
    version=1,
)


def cache_key(*, sources, grid, sensor, scale, **extra):
    body = dict(sources=sources, grid=grid, sensor=sensor, scale=scale, policy=POLICY, **extra)
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def decide(valid, cloud, total, sensor):
    if (
        sensor not in ("L30", "S30", "S1", "S2", "DW")
        or total <= 0
        or not 0 <= cloud <= valid <= total
    ):
        raise ValueError("Invalid quality counters or sensor")
    sparse = valid / total < POLICY["min_supported"]
    cloudy = sensor in ("L30", "S30", "S2") and valid > 0 and cloud / valid > POLICY["max_cloud"]
    return dict(
        keep=not (sparse or cloudy),
        reason="sparse" if sparse else "cloud" if cloudy else "keep",
        valid_ratio=valid / total,
        cloud_ratio=cloud / valid if valid else None,
    )


def summarize(valid, cloud, cell_size, sensor):
    valid = np.asarray(valid, dtype=bool)
    cloud = np.asarray(cloud, dtype=bool) & valid
    if valid.ndim != 2 or cloud.shape != valid.shape or cell_size < 1:
        raise ValueError("Matching two-dimensional masks required")
    result = []
    for y in range(0, valid.shape[0], cell_size):
        for x in range(0, valid.shape[1], cell_size):
            v = valid[y : y + cell_size, x : x + cell_size]
            c = cloud[y : y + cell_size, x : x + cell_size]
            n = int(v.size)
            vc = int(v.sum())
            cc = int(c.sum())
            result.append(
                dict(
                    row=y // cell_size,
                    col=x // cell_size,
                    height=v.shape[0],
                    width=v.shape[1],
                    valid=vc,
                    cloud=cc,
                    total=n,
                    scale=30,
                    **decide(vc, cc, n, sensor),
                )
            )
    return result


def approximate(valid, cloud, cell_size, sensor, scale):
    """Systematic coarse sampling oracle, not a proof of exact source coverage.

    Decode categories to booleans before calling. Edge weights represent the
    sampled area; the independent exact oracle measures decision errors.
    """
    if scale not in (100, 200):
        raise ValueError("Approximation scale must be 100m or 200m")
    valid = np.asarray(valid, dtype=bool)
    cloud = np.asarray(cloud, dtype=bool) & valid
    rows = []
    for cell in summarize(valid, cloud, cell_size, sensor):
        y, x = cell["row"] * cell_size, cell["col"] * cell_size
        height, width = cell["height"], cell["width"]
        xs = np.arange(0, width * 30, scale)
        ys = np.arange(0, height * 30, scale)
        dx = np.minimum(scale, width * 30 - xs)
        dy = np.minimum(scale, height * 30 - ys)
        ix = np.minimum(width - 1, ((xs + dx / 2) / 30).astype(int))
        iy = np.minimum(height - 1, ((ys + dy / 2) / 30).astype(int))
        weights = dy[:, None] * dx[None, :] / 900
        vc = min(
            cell["total"], int(round((valid[y + iy[:, None], x + ix[None, :]] * weights).sum()))
        )
        cc = min(vc, int(round((cloud[y + iy[:, None], x + ix[None, :]] * weights).sum())))
        rows.append(
            dict(cell, valid=vc, cloud=cc, scale=scale, **decide(vc, cc, cell["total"], sensor))
        )
    return rows


def compare_decisions(exact, estimate):
    left = {(r["row"], r["col"]): r for r in exact}
    right = {(r["row"], r["col"]): r for r in estimate}
    if left.keys() != right.keys():
        raise ValueError("Quality comparisons require the same cells")
    false_rejects = sum(left[k]["keep"] and not right[k]["keep"] for k in left)
    false_accepts = sum(not left[k]["keep"] and right[k]["keep"] for k in left)
    return dict(
        cells=len(left),
        false_rejects=false_rejects,
        false_accepts=false_accepts,
        valid_ratio_mean_abs_error=float(
            np.mean([abs(left[k]["valid_ratio"] - right[k]["valid_ratio"]) for k in left])
        )
        if left
        else 0,
    )
