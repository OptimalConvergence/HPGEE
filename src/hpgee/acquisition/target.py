"""Stable observation identities and transport-independent packing."""

import hashlib
import json
import re


def observation_key(source, sensor):
    props = source["metadata"].get("properties", {})
    if sensor == "S1" and all(
        props.get(k) is not None
        for k in ("platform_number", "orbitNumber_start", "missionDataTakeID")
    ):
        identity = [props[k] for k in ("platform_number", "orbitNumber_start", "missionDataTakeID")]
    elif sensor == "S30":
        match = re.search(
            r"(S2[A-C])_MSIL1C_(\d{8}T\d{6})_N\d+_(R\d+)_", props.get("PRODUCT_URI", "")
        )
        identity = list(match.groups()) if match else [source["scene"]["asset_id"]]
    elif sensor == "L30":
        matches = re.findall(
            r"(L[A-Z]\d{2})_[A-Z0-9]+_(\d{3})\d{3}_(\d{8})_", props.get("LANDSAT_PRODUCT_ID", "")
        )
        identity = sorted(set(matches)) if matches else [source["scene"]["asset_id"]]
    else:
        identity = [source["scene"]["asset_id"]]
    return (
        sensor
        + "-"
        + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    )


def pack_cells(cells, size):
    if size not in (128, 256, 512, 1024):
        raise ValueError("Request size must be 128/256/512/1024")
    grouped = {}
    step = size // 128
    for cell in cells:
        origin = (cell["row"] // step * step, cell["col"] // step * step)
        grouped.setdefault(origin, []).append(cell)
    return [
        dict(row=y, col=x, size=size, cells=sorted(values, key=lambda c: (c["row"], c["col"])))
        for (y, x), values in sorted(grouped.items())
    ]
