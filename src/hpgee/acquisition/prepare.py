"""Freeze explicit tasks; metadata-only mode performs no remote pixel reduction."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
from .catalog import atomic_json, ownership_predecessors
from .ee_quality import affine, collect, geometry
from .manifest import digest, freeze_task, freeze_unfiltered
from .quality import POLICY, policy_for
from .recipes import recipe
from .store import write_manifest


def prepare_tasks(ee, tasks, path, cache, *, selection="post", project, quality_workers=8):
    if selection not in ("post", "native", "guarded100") or not 1 <= quality_workers <= 40:
        raise ValueError("Unknown selection or invalid quality workers")
    began = time.monotonic()
    tasks = [dict(t) for t in tasks]
    path = Path(path)
    cache = Path(cache)
    if path.exists():
        raise FileExistsError(
            "Frozen target exists; reuse preparation.json or choose a new directory"
        )
    regions = {}
    for task in tasks:
        r = recipe(task["sensor"])
        grid = task["grid"]
        if task["sensor"] == "S1" and (affine(grid)[0] != 30 or affine(grid)[4] != -30):
            raise ValueError("S1 area-power recipe requires a 30m grid")
        regions.setdefault(task["region"], dict(id=task["region"], land_pixels=None))
        task["recipe_version"] = r["version"]
        task["sources"] = freeze_task(task, dict(key="identity", rows=[]))["sources"]
    # Geometric ownership is independent of QA; only duplicate observations
    # share ownership. Different times, platforms and orbits stay separate.
    grids = {t["region"]: t["grid"] for t in tasks}
    for task in ownership_predecessors(tasks):
        if not task.get("overlap_predecessors"):
            continue
        owner = geometry(ee, task["grid"])
        for previous in task["overlap_predecessors"]:
            owner = owner.difference(geometry(ee, grids[previous]), 1)
        for original in tasks:
            if (original["region"], original["sensor"], original["observation"]) == (
                task["region"],
                task["sensor"],
                task["observation"],
            ):
                original["owner_geometry"] = owner.transform("EPSG:4326", 1).getInfo()
                break

    def freeze(task):
        if selection == "post":
            return freeze_unfiltered(task)
        scale = abs(affine(task["grid"])[0]) if selection == "native" else 100
        stats = collect(ee, task, scale, cache / "quality")
        if selection == "guarded100":
            rows = [
                dict(
                    r,
                    keep=(
                        r["keep"]
                        or 0.05 <= r["valid_ratio"] <= 0.25
                        or r["cloud_ratio"] is not None
                        and 0.70 <= r["cloud_ratio"] <= 0.90
                    ),
                    valid=None,
                    cloud=None,
                )
                for r in stats["rows"]
            ]
            stats = dict(stats, key=digest(dict(source=stats["key"], guard="100m-v1")), rows=rows)
        return freeze_task(dict(task, selection=selection), stats)

    def frozen():
        with ThreadPoolExecutor(max_workers=quality_workers) as pool:
            for offset in range(0, len(tasks), quality_workers):
                yield from pool.map(freeze, tasks[offset : offset + quality_workers])

    header = dict(
        version=1,
        project=project,
        regions=list(regions.values()),
        expected_task_ids=[freeze_task(t, dict(key="identity", rows=[]))["id"] for t in tasks],
        sensors=list(dict.fromkeys(t["sensor"] for t in tasks)),
        selection=selection,
        policy=policy_for(tasks[0]) if tasks else POLICY,
        scope="explicit frozen observations; unknown land area until separately measured",
    )
    report = write_manifest(path, header, frozen())
    report.update(
        manifest=str(path.resolve()),
        preparation_seconds=time.monotonic() - began,
        selection=selection,
        exact_selection=selection != "guarded100",
        observations=len(tasks),
    )
    atomic_json(path.parent / "preparation.json", report)
    return report
