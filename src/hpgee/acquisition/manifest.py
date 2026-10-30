"""Freeze logical quality cells once; HTTP packing and host assignment are separate."""

import hashlib
import json
from .ee_quality import cell_grid
from .target import pack_cells
from .quality import POLICY
from .recipes import recipe


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def task_identity(task):
    return digest(
        dict(
            region=task["region"],
            observation=task["observation"],
            sensor=task["sensor"],
            grid=task["grid"],
            sources=task["sources"],
            owner=task.get("owner_geometry"),
            land=task.get("land_asset"),
            recipe=task.get("recipe_version") or recipe(task["sensor"])["version"],
            policy=POLICY,
            mask_version="fixed-land-grid-v3",
        )
    )


def freeze_task(task, quality):
    sources = []
    for source in task["sources"]:
        reference = dict(scene=source["scene"])
        if "metadata" in source:
            reference["metadata_sha256"] = digest(source["metadata"])
        elif "metadata_sha256" in source:
            reference["metadata_sha256"] = source["metadata_sha256"]
        sources.append(reference)
    task = dict(task, sources=sources, quality_key=quality["key"])
    task["id"] = task_identity(task)
    selected = []
    for row in quality["rows"]:
        if row["keep"]:
            identity = dict(task=task["id"], row=row["row"], col=row["col"], quality=quality["key"])
            selected.append(dict(row, id=digest(identity)))
    task["cells"] = selected
    task["quality_summary"] = dict(
        required=len(selected),
        examined=len(quality["rows"]),
        skipped_sparse=sum(
            r["valid"] is not None and r["valid"] / r["total"] < 0.15 for r in quality["rows"]
        ),
        skipped_cloud=sum(r.get("reason") == "cloud" for r in quality["rows"]),
        valid_pixels=sum(r["valid"] or 0 for r in selected),
        cloud_pixels=sum(r["cloud"] or 0 for r in selected),
        land_pixels=sum(r.get("land", 0) for r in quality["rows"]),
    )
    return task


def requests_for_task(task, size):
    if (
        task.get("recipe_version", recipe(task["sensor"])["version"])
        != recipe(task["sensor"])["version"]
    ):
        raise ValueError("Frozen recipe differs from current image formula; prepare a new target")
    result = []
    height, width = (task["grid"]["dimensions"][k] for k in ("height", "width"))
    for pack in pack_cells(task["cells"], size):
        h, w = min(size, height - pack["row"] * 128), min(size, width - pack["col"] * 128)
        grid = cell_grid(task["grid"], pack["row"], pack["col"], h, w)
        import numpy as np
        from hpgee.transfer.raw_download import pack_limit

        r = recipe(task["sensor"])
        pack_limit(w, h, len(r["bands"]), 1, itemsize=np.dtype(r["dtype"]).itemsize)
        uid = digest(
            dict(
                task=task["id"],
                quality=task["quality_key"],
                size=size,
                row=pack["row"],
                col=pack["col"],
                cells=[c["id"] for c in pack["cells"]],
            )
        )
        result.append(
            dict(
                pack,
                id=uid,
                task=task["id"],
                grid=grid,
                sensor=task["sensor"],
                observation=task["observation"],
                region=task["region"],
            )
        )
    return result


def request_owner(request, hosts):
    if hosts < 1:
        raise ValueError("At least one host required")
    # Every allowed transport size divides this 1024px macrocell. Consequently
    # a fixed logical cell stays on the same host when request packing changes.
    key = dict(task=request["task"], row=request["row"] // 8, col=request["col"] // 8)
    return int(digest(key)[:16], 16) % hosts


def freeze_unfiltered(task):
    """Freeze geometry only; exact QA is measured from downloaded pixels."""
    from .ee_quality import cells

    task = dict(task, selection="post")
    quality = dict(
        key=digest(dict(mode="post", policy=POLICY, mask="fixed-land-grid-v3")),
        rows=[
            dict(c, total=c["height"] * c["width"], valid=None, cloud=None, land=0, keep=True)
            for c in cells(task["grid"])
        ],
    )
    return freeze_task(task, quality)
