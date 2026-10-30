"""Compact frozen tasks, staged once on local disk; only a small task cache uses RAM."""

import functools
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from .manifest import digest, task_identity


def derived_cell(task, cell):
    row, col = cell["row"], cell["col"]
    grid = task["grid"]["dimensions"]
    h, w = min(128, grid["height"] - row * 128), min(128, grid["width"] - col * 128)
    if min(row, col) < 0 or min(h, w) <= 0:
        raise ValueError("Cell outside the frozen grid")
    return dict(
        id=digest(dict(task=task["id"], row=row, col=col, quality=task["quality_key"])),
        height=h,
        width=w,
        total=h * w,
        valid_ratio=cell["valid"] / (h * w) if cell["valid"] is not None else None,
        cloud_ratio=cell["cloud"] / cell["valid"] if cell["valid"] else None,
    )


def compact(task):
    if task_identity(task) != task["id"]:
        raise ValueError("Task identity differs from frozen sources")
    rows = task["cells"]
    keys = set(rows[0]) if rows else set()
    if any(set(row) != keys for row in rows):
        raise ValueError("Inconsistent cell schema")
    derived = sorted(keys & {"id", "height", "width", "total", "valid_ratio", "cloud_ratio"})
    for row in rows:
        expected = derived_cell(task, row)
        if any(expected[k] != row[k] for k in derived):
            raise ValueError("Frozen cell geometry or identity is inconsistent")
    common = {
        k: rows[0][k] for k in keys - set(derived) if all(row[k] == rows[0][k] for row in rows)
    }
    columns = sorted(keys - set(derived) - set(common))
    value = {k: v for k, v in task.items() if k != "cells"}
    value.update(
        cell_schema=dict(derived=derived, common=common, columns=columns),
        cell_values=[[row[k] for k in columns] for row in rows],
    )
    return value


def expand(value):
    task = {k: v for k, v in value.items() if k not in ("cell_schema", "cell_values")}
    if task_identity(task) != task["id"]:
        raise ValueError("Task identity differs from frozen sources")
    schema = value["cell_schema"]
    rows = []
    for fields in value["cell_values"]:
        if len(fields) != len(schema["columns"]):
            raise ValueError("Truncated cell record")
        row = dict(schema["common"], **dict(zip(schema["columns"], fields)))
        values = derived_cell(task, row)
        row.update({k: values[k] for k in schema["derived"]})
        rows.append(row)
    task["cells"] = rows
    return task


def summarize_record(value, state):
    uid = value["id"]
    region = value["region"]
    if uid in state["ids"]:
        raise ValueError("Duplicate task in fixed manifest")
    if uid not in state["expected"]:
        raise ValueError("Undeclared observation in fixed manifest")
    if region not in state["regions"]:
        raise ValueError("Undeclared target region")
    state["ids"].add(uid)
    state["required_cells"] += len(value["cell_values"])


def initial_state(header):
    ids = [r["id"] for r in header["regions"]]
    expected = header["expected_task_ids"]
    areas = {r["id"]: r["land_pixels"] for r in header["regions"]}
    if (
        len(set(ids)) != len(ids)
        or len(set(expected)) != len(expected)
        or header.get("version") != 1
        or any(n is not None and (not isinstance(n, int) or n < 0) for n in areas.values())
    ):
        raise ValueError("Invalid target header")
    return dict(
        ids=set(),
        regions=set(ids),
        expected=set(expected),
        required_cells=0,
        region_land_pixels=areas,
    )


def finish_state(state):
    if state["ids"] != state["expected"]:
        raise ValueError("Missing prepared observations in fixed manifest")
    return dict(
        tasks=len(state["ids"]),
        required_cells=state["required_cells"],
        region_land_pixels=state["region_land_pixels"],
        target_area_km2=(
            sum(state["region_land_pixels"].values()) * 0.0009
            if all(n is not None for n in state["region_land_pixels"].values())
            else None
        ),
        area_basis="unique region land pixels on nominal 30m grids",
    )


def write_manifest(path, header, tasks):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    state = initial_state(header)
    checksum = hashlib.sha256()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=path.name + ".partial-", delete=False
        ) as raw:
            temporary = Path(raw.name)
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as stream:

                def emit(value):
                    line = (
                        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode()
                    checksum.update(line)
                    stream.write(line)

                emit(dict(header, type="header"))
                for task in tasks:
                    value = compact(task)
                    summarize_record(value, state)
                    emit(value)
            raw.flush()
            os.fsync(raw.fileno())
        report = dict(
            finish_state(state), id=checksum.hexdigest(), compressed_bytes=temporary.stat().st_size
        )
        os.link(temporary, path)  # Atomic publication; an existing target is never overwritten.
        return report
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class TaskStore:
    def __init__(self, path, scratch, expected_digest):
        self.index = {}
        self.fd = None
        self.id = expected_digest
        with tempfile.NamedTemporaryFile(dir=scratch, prefix="task-stage-", delete=False) as output:
            self.path = Path(output.name)
            checksum = hashlib.sha256()
            try:
                with gzip.open(path, "rb") as source:
                    first = source.readline()
                    checksum.update(first)
                    self.header = json.loads(first)
                    if self.header.get("type") != "header":
                        raise ValueError("Missing fixed target header")
                    state = initial_state(self.header)
                    for line in source:
                        checksum.update(line)
                        value = json.loads(line)
                        summarize_record(value, state)
                        self.index[value["id"]] = (output.tell(), len(line))
                        output.write(line)
                if checksum.hexdigest() != expected_digest:
                    raise ValueError("Fixed manifest digest mismatch")
                self.summary = finish_state(state)
            except BaseException:
                self.path.unlink(missing_ok=True)
                raise
        self.fd = os.open(self.path, os.O_RDONLY)

    @functools.lru_cache(maxsize=16)
    def __getitem__(self, uid):
        offset, size = self.index[uid]
        return expand(json.loads(os.pread(self.fd, size, offset)))

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.__getitem__.cache_clear()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.path.unlink(missing_ok=True)
