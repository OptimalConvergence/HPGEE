"""Full source metadata in bounded pages; only copies of one observation overlap."""

import json
from pathlib import Path
import uuid
from .manifest import digest
from .target import observation_key


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def cached_value(root, identity, load, *, cached=True):
    root = Path(root)
    index = root / "index" / (digest(identity) + ".json")
    if cached and index.exists():
        key = json.loads(index.read_text())["object"]
        value = json.loads((root / "objects" / (key + ".json")).read_text())
        if digest(value) != key:
            raise ValueError("Metadata cache content digest mismatch")
        return value
    from hpgee.runtime.retry import retry_read

    value = retry_read(load, attempts=2, base_seconds=1, cap_seconds=1)
    key = digest(value)
    path = root / "objects" / (key + ".json")
    if not path.exists():
        atomic_json(path, value)
    atomic_json(index, {"object": key})
    return value


def list_images(ee, parent, start, end, region, cache, *, cached=True):
    # A scene beginning inside the window is retained even if its end crosses
    # midnight. The legacy listImages endTime parameter filters acquisition end.
    params = dict(
        parent=parent,
        pageSize=100,
        view="FULL",
        region=json.dumps(region, sort_keys=True),
        filter=f'startTime >= "{start}T00:00:00Z" AND startTime < "{end}T00:00:00Z"',
    )

    def load():
        request = dict(params)
        images = {}
        tokens = set()
        while True:
            result = ee.data.listImages(request)
            for image in result.get("images", []):
                key = image.get("id") or image.get("name")
                if not key:
                    raise ValueError("An image listing lacks its source identity")
                if key in images and images[key] != image:
                    raise ValueError("Source metadata changed during pagination")
                images[key] = image
            if len(images) > 4096:
                raise ValueError("The bounded acquisition window exceeds4096 images")
            token = result.get("nextPageToken")
            if not token:
                break
            if token in tokens:
                raise ValueError("Repeated image-list page token")
            tokens.add(token)
            request = dict(params, pageToken=token)
        return [images[key] for key in sorted(images)]

    return cached_value(cache, params, load, cached=cached)


def group_observations(region, sensor, sources):
    groups = {}
    seen = {}
    for source in sources:
        asset = source["scene"]["asset_id"]
        if asset in seen:
            if seen[asset] != source:
                raise ValueError("Conflicting frozen source metadata")
            continue
        seen[asset] = source
        key = observation_key(source, sensor)
        groups.setdefault(key, []).append(source)
    tasks = [
        dict(
            region=region,
            sensor=sensor,
            observation=key,
            sources=sorted(values, key=lambda s: s["scene"]["asset_id"]),
        )
        for key, values in groups.items()
    ]
    return sorted(
        tasks,
        key=lambda t: (min(s["scene"].get("time_ms", 0) for s in t["sources"]), t["observation"]),
    )


def ownership_predecessors(tasks):
    previous = {}
    result = []
    for task in tasks:
        regions = previous.setdefault((task["sensor"], task["observation"]), [])
        if task["region"] in regions:
            raise ValueError("Duplicate region/observation before overlap ownership")
        result.append(dict(task, overlap_predecessors=list(regions)))
        regions.append(task["region"])
    return result
