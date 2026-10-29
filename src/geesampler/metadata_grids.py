"""Download authoritative reference grids and preserve their source geometries."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from .storage import require_local_sqlite

SOURCES = {
    "wrs2": "https://d9-wret.s3.us-west-2.amazonaws.com/assets/palladium/production/s3fs-public/atoms/files/WRS2_descending.zip",
    "mgrs": "https://sentiwiki.copernicus.eu/__attachments/a_69a662aa1cb30487acc66009f09b1dcfa9e4e32f92af1c3b2dd1fc8a3c011738/S2A_OPER_GIP_TILPAR_MPC__20151209T095117_V20150622T000000_21000101T000000_B00.zip",
}


def fetch_archive(url: str, path: Path) -> dict:
    """Keep valid downloaded archives for reuse; atomic promotion after ZIP CRC check."""
    if not path.exists():
        temp = path.with_suffix(".partial")
        with requests.get(url, stream=True, timeout=(20, 120)) as response:
            response.raise_for_status()
            with temp.open("wb") as stream:
                for block in response.iter_content(1024 * 1024):
                    stream.write(block)
        with zipfile.ZipFile(temp) as archive:
            if archive.testzip() is not None:
                raise ValueError("Downloaded grid archive failed CRC validation")
        temp.replace(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"url": url, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def mgrs_features(path: Path):
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    with zipfile.ZipFile(path) as archive:
        files = [p for p in archive.namelist() if p.lower().endswith(".kml")]
        if len(files) != 1:
            raise ValueError("Expected one Sentinel-2 tiling KML")
        with archive.open(files[0]) as stream:
            for _, element in ET.iterparse(stream, events=("end",)):
                if element.tag != "{" + ns["k"] + "}Placemark":
                    continue
                code = (element.findtext("k:name", namespaces=ns) or "").strip()
                if not re.fullmatch(r"\d{2}[A-Z]{3}", code):
                    raise ValueError(f"Unexpected MGRS tile name: {code[:50]}")
                polygons = []
                for polygon in element.findall(".//k:Polygon", ns):
                    rings = []
                    for coords in polygon.findall(".//k:coordinates", ns):
                        rings.append(
                            [
                                [float(v) for v in point.split(",")[:2]]
                                for point in coords.text.split()
                            ]
                        )
                    polygons.append(rings)
                if not polygons:
                    raise ValueError("Tile lacks polygon geometry")
                yield code, {"type": "MultiPolygon", "coordinates": polygons}
                element.clear()


def build_grids(directory: str | Path) -> dict:
    """Build a grid reference library; grid cells do not imply scene availability."""
    from .catalog import _bounds

    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "grids.sqlite"
    temporary = directory / "grids.build.sqlite"
    require_local_sqlite(database)
    sources = {
        name: fetch_archive(url, directory / (name + ".zip")) for name, url in SOURCES.items()
    }
    if temporary.exists():
        temporary.unlink()
    with sqlite3.connect(temporary) as db:
        db.executescript(
            "CREATE TABLE cells(id INTEGER PRIMARY KEY, grid TEXT, code TEXT, geometry_json TEXT); CREATE INDEX grid_code ON cells(grid,code); CREATE VIRTUAL TABLE cell_rtree USING rtree(id,minx,maxx,miny,maxy);"
        )
        import geopandas as gpd
        from shapely.geometry import mapping

        wrs = gpd.read_file("zip://" + str(directory / "wrs2.zip")).to_crs("EPSG:4326")

        def wrs_features():
            for _, row in wrs.iterrows():
                yield f"{int(row['PATH']):03d}/{int(row['ROW']):03d}", mapping(row.geometry)

        for name, features in [
            ("wrs2", wrs_features()),
            ("mgrs", mgrs_features(directory / "mgrs.zip")),
        ]:
            for code, geometry in features:
                lo, bottom, hi, top = _bounds(geometry)
                cursor = db.execute(
                    "INSERT INTO cells(grid,code,geometry_json) VALUES (?,?,?)",
                    (name, code, json.dumps(geometry)),
                )
                db.execute(
                    "INSERT INTO cell_rtree VALUES (?,?,?,?,?)",
                    (cursor.lastrowid, lo, hi, bottom, top),
                )
            sources[name]["features"] = db.execute(
                "SELECT COUNT(*) FROM cells WHERE grid=?", (name,)
            ).fetchone()[0]
            sources[name]["unique_codes"] = db.execute(
                "SELECT COUNT(DISTINCT code) FROM cells WHERE grid=?", (name,)
            ).fetchone()[0]
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Grid SQLite integrity check failed")
    temporary.replace(database)
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": str(database),
        "sources": sources,
    }
    (directory / "grids.json").write_text(json.dumps(result, indent=2))
    return result
