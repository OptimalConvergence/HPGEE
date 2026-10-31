"""Accept only the frozen target cells; failed/partial requests never complete them."""

import hashlib
import json
import os
from pathlib import Path
import uuid
import numpy as np

# Receipts keep this field so resume can reject the retired nodata/packed layout.
LAYOUT = "full"


def validate_array(values, request):
    mass_error = None
    values = np.asarray(values)
    sensor = request["sensor"]
    h, w = (request["grid"]["dimensions"][k] for k in ("height", "width"))
    if values.ndim != 3 or values.shape[1:] != (h, w):
        raise ValueError("Response shape differs from request")
    if sensor in ("L30", "S30"):
        if values.shape[0] != 8 or values.dtype != np.int16:
            raise ValueError("Eight Int16 HLS bands required")
        qa = values[6]
        raw_bits = values[7]
        if np.any((raw_bits < 0) | (raw_bits > 255)):
            raise ValueError("Invalid HLS source bits")
        bits = raw_bits.astype("uint8")
        if np.any(bits & 128):
            raise ValueError("HLS Int16 overflow flag is set")
        qa_valid = (bits & 64) != 0
        if np.any((qa[qa_valid] < 0) | (qa[qa_valid] > 255)):
            raise ValueError("Invalid Fmask")
        valid = (bits & 127) == 127
        cloud = valid & ((qa & 2) != 0)
    elif sensor == "S1":
        if values.shape[0] != 4 or values.dtype != np.float32:
            raise ValueError("Four Float32 S1 bands required")
        science = values[:2]
        fraction = values[3]
        bits = values[2]
        valid = fraction > 0
        cloud = np.zeros_like(valid)
        if (
            not np.isfinite(fraction).all()
            or np.any(fraction < 0)
            or np.any(fraction > 1 + 1e-5)
            or not np.array_equal(bits, valid.astype("uint8") * 3)
        ):
            raise ValueError("S1 support fraction and validity bits disagree")
        if not np.isfinite(science[:, valid]).all() or np.any(science[:, valid] < 0):
            raise ValueError("Invalid supported S1 power")
    elif sensor == "S2":
        if values.shape[0] != 14 or values.dtype != np.uint16:
            raise ValueError("Fourteen UInt16 S2 bands required")
        bits = values[-1]
        valid = bits == 8191
        if np.any(bits > 8191):
            raise ValueError("Invalid S2 support bits")
        scl = values[-2]
        if np.any(scl[valid] > 11):
            raise ValueError("Invalid categorical SCL")
        cloud = valid & np.isin(scl, [8, 9, 10])
    elif sensor == "DW":
        if values.shape[0] != 11 or values.dtype != np.float32:
            raise ValueError("Eleven Float32 DW bands required")
        bits = values[-1]
        valid = bits == 1
        cloud = np.zeros_like(valid)
        if np.any((bits != 0) & (bits != 1)):
            raise ValueError("Invalid DW support bits")
        probs = values[:9, valid]
        label = values[9, valid]
        if not np.isfinite(probs).all() or np.any(probs < 0) or np.any(probs > 1):
            raise ValueError("Invalid DW probabilities")
        # Real catalog values can sum to0.999711; retain them unchanged.
        mass_error = float(np.max(np.abs(probs.sum(axis=0) - 1))) if probs.size else None
        if (
            not np.isfinite(label).all()
            or np.any(label < 0)
            or np.any(label > 8)
            or np.any(label != np.round(label))
        ):
            raise ValueError("Invalid categorical DW label")
    else:
        raise ValueError("Unknown sensor")
    selected = np.zeros((h, w), dtype=bool)
    vc = cc = 0
    qv = qc = 0
    ids = []
    kept = []
    rejected = []
    from .quality import decide

    size = request.get("cell_pixels", 128)
    if size not in (128, 256):
        raise ValueError("Unknown frozen quality cell size")
    for cell in request["cells"]:
        y = (cell["row"] - request["row"]) * size
        x = (cell["col"] - request["col"]) * size
        height, width = cell["height"], cell["width"]
        if min(y, x) < 0 or y + height > h or x + width > w:
            raise ValueError("Target cell outside request")
        window = np.s_[y : y + height, x : x + width]
        selected[window] = True
        v = int(valid[window].sum())
        c = int(cloud[window].sum())
        if cell.get("valid") is not None and (v, c) != (cell["valid"], cell["cloud"]):
            raise ValueError("Delivered source/cloud support differs from frozen quality counts")
        vc += v
        cc += c
        ids.append(cell["id"])
        if decide(v, c, height * width, sensor)["keep"]:
            kept.append(cell["id"])
            qv += v
            qc += c
        else:
            rejected.append(cell["id"])
    if np.any(bits[~selected] != 0):
        raise ValueError("Unselected cells contain supported output pixels")
    return dict(
        cells=sorted(ids),
        valid_pixels=vc,
        cloud_pixels=cc,
        clear_pixels=vc - cc,
        quality_kept_cells=len(kept),
        quality_rejected_cells=len(rejected),
        quality_kept_ids=kept,
        quality_valid_pixels=qv,
        quality_clear_pixels=qv - qc,
        probability_mass_max_error=mass_error,
    )


def verified(request, destination):
    root = Path(destination)
    path = root / (request["id"] + ".json")
    if not path.exists():
        return None
    row = json.loads(path.read_text())
    data = root / (request["id"] + ".tif")
    if row.get("validator") != "fixed-land-grid-v3-recipe-v1":
        return None
    if (
        row["request_id"] != request["id"]
        or row["encoding"] != LAYOUT
        or row["cells"] != sorted(c["id"] for c in request["cells"])
    ):
        raise ValueError("Receipt belongs to another fixed target or layout")
    if not data.exists() or hashlib.sha256(data.read_bytes()).hexdigest() != row["sha256"]:
        return None
    return row


def publish(payload, request, destination):
    from rasterio import Affine, CRS
    from rasterio.io import MemoryFile
    from .ee_quality import affine
    from .images import output_bands

    with MemoryFile(payload) as memory:
        with memory.open() as dataset:
            grid = request["grid"]
            dims = grid["dimensions"]
            if (
                dataset.crs != CRS.from_user_input(grid["crsCode"])
                or (dataset.width, dataset.height) != (dims["width"], dims["height"])
                or not np.allclose(
                    tuple(dataset.transform), tuple(Affine(*affine(grid))), rtol=0, atol=1e-7
                )
            ):
                raise ValueError("Response grid differs from frozen request")
            if any(dataset.descriptions) and list(dataset.descriptions) != output_bands(
                request["sensor"]
            ):
                raise ValueError("Response band order differs from frozen request")
            row = validate_array(dataset.read(), request)
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    data = root / (request["id"] + ".tif")
    receipt = data.with_suffix(".json")
    temporary = data.with_name(data.name + "." + uuid.uuid4().hex + ".partial")
    temp_receipt = receipt.with_suffix(".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, data)
        row.update(
            request_id=request["id"],
            encoding=LAYOUT,
            validator="fixed-land-grid-v3-recipe-v1",
            sha256=hashlib.sha256(payload).hexdigest(),
            bytes=len(payload),
        )
        temp_receipt.write_text(json.dumps(row, sort_keys=True) + "\n")
        temp_receipt.replace(receipt)
    finally:
        temporary.unlink(missing_ok=True)
        temp_receipt.unlink(missing_ok=True)
    return row
