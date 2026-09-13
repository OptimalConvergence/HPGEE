"""Global paginated metadata scans with atomic SQLite page checkpoints.

No image pixels or server-side image computation are requested. Date windows
are half-open UTC intervals. An exhausted page chain proves coverage only for
that collection/window as observed during this scan, not perpetual immutability.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from .catalog import S2SceneCatalog, SceneRecord, _bounds
from .retry import retry_read

COLLECTIONS = {
    "s2": "COPERNICUS/S2_SR_HARMONIZED",
    "landsat4": "LANDSAT/LT04/C02/T1_L2",
    "landsat5": "LANDSAT/LT05/C02/T1_L2",
    "landsat7": "LANDSAT/LE07/C02/T1_L2",
    "landsat9": "LANDSAT/LC09/C02/T1_L2",
    "hlsl30": "NASA/HLS/HLSL30/v002",
    "hlss30": "NASA/HLS/HLSS30/v002",
    "viirs": "NASA/VIIRS/002/VNP09GA",
    "gedi": "LARSE/GEDI/GEDI02_A_002_MONTHLY",
    "gedi-index": "LARSE/GEDI/GEDI02_A_002_INDEX",
}


def utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def grid_key(collection: str, asset: dict) -> str:
    props = asset.get("properties", {})
    tile = props.get("MGRS_TILE") or props.get("MGRS_TILE_ID")
    if tile:
        return str(tile).removeprefix("T")
    if "WRS_PATH" in props and "WRS_ROW" in props:
        return f"{int(props['WRS_PATH']):03d}/{int(props['WRS_ROW']):03d}"
    if collection.startswith(("COPERNICUS/S2", "NASA/HLS")):
        match = re.search(r"(?:^|[/_])T([0-9]{2}[A-Z]{3})(?:_|$)", asset["id"])
        if match:
            return match.group(1)
        raise ValueError("Missing MGRS key")
    if collection.startswith("LANDSAT/"):
        raise ValueError("Missing WRS path/row")
    # EE's VIIRS daily mosaics and GEDI monthly rasters are not native source tiles.
    return "global"


def scene_record(collection: str, asset: dict) -> SceneRecord:
    asset_id = asset["id"]
    asset_collection = (
        collection.removesuffix("_INDEX") if collection == COLLECTIONS["gedi-index"] else collection
    )
    if not asset_id.startswith(asset_collection + "/") or not asset.get("startTime"):
        raise ValueError("Asset has unexpected collection or missing acquisition time")
    geometry = asset.get("geometry")
    bounds = _bounds(geometry)
    unbounded = (
        bounds is None
        and collection in (COLLECTIONS["viirs"], COLLECTIONS["gedi"])
        and "Infinity" in json.dumps(geometry)
    )
    if unbounded:
        bounds = (-180.0, -90.0, 180.0, 90.0)
    if bounds is None:
        raise ValueError("Image metadata lacks a spatial footprint")
    props = dict(asset.get("properties", {}))
    props["system:footprint"] = geometry
    if unbounded:
        props["geesampler:footprint_kind"] = "unbounded_mosaic_conservative_world_bbox"
        props["geesampler:band_grids"] = [
            {k: v for k, v in band.items() if k in ("id", "grid", "dimensions")}
            for band in asset.get("bands", [])
        ]
    props["system:index"] = asset_id.rsplit("/", 1)[1]
    millis = int(utc(asset["startTime"]).timestamp() * 1000)
    props["system:time_start"] = millis
    props["geesampler:update_time"] = asset.get("updateTime")
    cloud = next(
        (
            props[k]
            for k in ("CLOUDY_PIXEL_PERCENTAGE", "CLOUD_COVER", "CLOUD_COVERAGE")
            if props.get(k) is not None
        ),
        None,
    )
    return SceneRecord(
        collection,
        asset_id,
        props["system:index"],
        millis,
        grid_key(collection, asset),
        cloud,
        bounds,
        props,
    )


class MetadataScanner:
    """Store native grid keys in the catalog's legacy ``mgrs_tile`` column.

    Reuses the existing S2 schema so incremental S2 rows are immediately usable
    by existing resolvers. For Landsat this column holds zero-padded path/row;
    VIIRS/GEDI mosaic assets use 'global'. Each catalog should have one scanner
    process; its worker threads serialize SQLite transactions.
    """

    def __init__(self, catalog: S2SceneCatalog, ee_module: Any):
        self.catalog = catalog
        self.ee = ee_module
        with catalog._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata_scans (
                    collection TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                    page_token TEXT, complete INTEGER NOT NULL DEFAULT 0,
                    pages INTEGER NOT NULL DEFAULT 0, rows_seen INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(collection,start,end)
                );
                CREATE TABLE IF NOT EXISTS metadata_scan_tokens (
                    collection TEXT, start TEXT, end TEXT, token TEXT,
                    PRIMARY KEY(collection,start,end,token)
                );
            """)

    def _remaining(self, collection: str, start: datetime, end: datetime, *, include_pending=False):
        with self.catalog._connect() as db:
            done = db.execute(
                "SELECT start,end FROM metadata_scans WHERE collection=?"
                + ("" if include_pending else " AND complete=1")
                + " ORDER BY start",
                (collection,),
            ).fetchall()
        cursor = start
        for row in done:
            a, b = utc(row[0]), utc(row[1])
            if b <= cursor or a >= end:
                continue
            if a > cursor:
                yield cursor, min(a, end)
            cursor = max(cursor, b)
        if cursor < end:
            yield cursor, end

    def _list_page(self, collection, start, end, request):
        if collection != COLLECTIONS["gedi-index"]:
            return self.ee.data.listImages(dict(request))
        # GEDI's index holds orbit-granule table footprints, not a fixed tiling.
        iso = lambda value: utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        features = (
            self.ee.FeatureCollection(collection)
            .filter(self.ee.Filter.gte("time_start", iso(start)))
            .filter(self.ee.Filter.lt("time_start", iso(end)))
        )
        args = {"expression": features, "pageSize": request["pageSize"]}
        if request.get("pageToken"):
            args["pageToken"] = request["pageToken"]
        response = self.ee.data.computeFeatures(args)
        if not isinstance(response.get("features"), list):
            raise TypeError("Malformed GEDI table-index response")
        images = []
        for feature in response["features"]:
            props = feature["properties"]
            images.append(
                {
                    "id": props["table_id"],
                    "startTime": props["time_start"],
                    "geometry": feature.get("geometry"),
                    "properties": props,
                }
            )
        return {"images": images, "nextPageToken": response.get("nextPageToken")}

    def scan_window(self, collection: str, start: str, end: str, *, page_size: int = 1000) -> dict:
        """Commit scenes and cursor together; interrupted windows resume exactly."""
        if utc(start) >= utc(end) or not 1 <= page_size <= 1000:
            raise ValueError("Invalid date window or page size")
        start, end = utc(start).isoformat(), utc(end).isoformat()
        key = (collection, start, end)
        now = datetime.now(timezone.utc).isoformat()
        with self.catalog._write_lock, self.catalog._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO metadata_scans(collection,start,end,updated_at) VALUES (?,?,?,?)",
                (*key, now),
            )
            state = db.execute(
                "SELECT * FROM metadata_scans WHERE collection=? AND start=? AND end=?", key
            ).fetchone()
        if state["complete"]:
            return dict(state)
        token = state["page_token"]
        while True:
            # listImages(endTime=...) filters asset END time and can drop daily/monthly
            # composites at a chunk boundary. Filter acquisition START explicitly.
            request = {
                "parent": collection,
                "filter": f'startTime >= "{start}" AND startTime < "{end}"',
                "pageSize": page_size,
            }
            if token:
                request["pageToken"] = token
            began = time.monotonic()
            response = retry_read(
                lambda request=request: self._list_page(collection, start, end, request)
            )
            if not isinstance(response.get("images"), list):
                raise TypeError("Malformed listImages response; coverage is not complete")
            scenes = [scene_record(collection, a) for a in response["images"]]
            lo, hi = int(utc(start).timestamp() * 1000), int(utc(end).timestamp() * 1000)
            if any(not lo <= s.acquired_millis < hi for s in scenes):
                raise ValueError("Server returned an image outside the requested interval")
            if len({s.asset_id for s in scenes}) != len(scenes):
                raise ValueError("Duplicate assets within one page")
            next_token = response.get("nextPageToken") or None
            now = datetime.now(timezone.utc).isoformat()
            with self.catalog._write_lock, self.catalog._connect() as db:
                if next_token:
                    if (
                        next_token == token
                        or db.execute(
                            "SELECT 1 FROM metadata_scan_tokens WHERE collection=? AND start=? AND end=? AND token=?",
                            (*key, next_token),
                        ).fetchone()
                    ):
                        raise ValueError("Repeated pagination token; coverage is not complete")
                    db.execute(
                        "INSERT INTO metadata_scan_tokens VALUES (?,?,?,?)", (*key, next_token)
                    )
                for scene in scenes:
                    self.catalog._upsert_scene(db, scene, now)
                db.execute(
                    "UPDATE metadata_scans SET page_token=?,complete=?,pages=pages+1,rows_seen=rows_seen+?,updated_at=? WHERE collection=? AND start=? AND end=?",
                    (next_token, int(next_token is None), len(scenes), now, *key),
                )
                if next_token is None:
                    db.execute(
                        "INSERT OR REPLACE INTO coverage(collection,mgrs_tile,start_millis,end_millis,fetched_at) VALUES (?,?,?,?,?)",
                        (collection, "*", lo, hi, now),
                    )
                    db.execute(
                        "DELETE FROM metadata_scan_tokens WHERE collection=? AND start=? AND end=?",
                        key,
                    )
            print(
                json.dumps(
                    {
                        "metadata_page": collection,
                        "start": start,
                        "end": end,
                        "rows": len(scenes),
                        "seconds": time.monotonic() - began,
                        "complete": next_token is None,
                    }
                ),
                flush=True,
            )
            if next_token is None:
                with self.catalog._connect() as db:
                    return dict(
                        db.execute(
                            "SELECT * FROM metadata_scans WHERE collection=? AND start=? AND end=?",
                            key,
                        ).fetchone()
                    )
            token = next_token

    def sync(self, collection: str, start: str, end: str, **kwargs) -> dict:
        # A separate SQLite file provides a cross-process writer lock without
        # holding the scene database locked during network requests.
        import sqlite3

        lock = sqlite3.connect(str(self.catalog.path) + ".scan-lock", timeout=0)
        try:
            lock.execute("BEGIN EXCLUSIVE")
            return self._sync_unlocked(collection, start, end, **kwargs)
        finally:
            lock.close()

    def _sync_unlocked(
        self,
        collection: str,
        start: str,
        end: str,
        *,
        workers: int = 2,
        chunk_days: int = 31,
        page_size: int = 1000,
    ) -> dict:
        a, b = utc(start), utc(end)
        if a >= b or workers < 1 or chunk_days < 1 or not 1 <= page_size <= 1000:
            raise ValueError("Invalid scan parameters")
        collection = COLLECTIONS.get(collection, collection)
        windows = []
        # Resume original incomplete windows before making a differently sized plan.
        with self.catalog._connect() as db:
            pending = db.execute(
                "SELECT start,end FROM metadata_scans WHERE collection=? AND complete=0 ORDER BY start",
                (collection,),
            ).fetchall()
        for row in pending:
            x, y = utc(row[0]), utc(row[1])
            if x < b and y > a:
                if x < a or y > b:
                    raise ValueError(
                        "Requested range clips an incomplete window; resume its original interval first"
                    )
                windows.append((row[0], row[1]))
        for x, y in self._remaining(collection, a, b, include_pending=True):
            while x < y:
                z = min(x + timedelta(days=chunk_days), y)
                windows.append((x.isoformat(), z.isoformat()))
                x = z
        now = datetime.now(timezone.utc).isoformat()
        with self.catalog._write_lock, self.catalog._connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO metadata_scans(collection,start,end,updated_at) VALUES (?,?,?,?)",
                [(collection, x, y, now) for x, y in windows],
            )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(
                pool.map(
                    lambda pair: self.scan_window(collection, *pair, page_size=page_size), windows
                )
            )
        remaining = list(self._remaining(collection, a, b))
        return {
            "collection": collection,
            "start": a.isoformat(),
            "end": b.isoformat(),
            "complete": not remaining,
            "new_windows": len(windows),
        }


def scan_status(path) -> dict:
    """Read scan progress without initializing or modifying the catalog."""
    import sqlite3
    from pathlib import Path

    with sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT collection,COUNT(*) AS windows,SUM(complete) AS completed_windows,"
                "SUM(pages) AS pages,SUM(rows_seen) AS rows_seen,MAX(updated_at) AS last_update "
                "FROM metadata_scans GROUP BY collection"
            )
        ]
    return {
        "collections": rows,
        "note": "rows_seen counts paginated records; coverage is per completed window",
    }
