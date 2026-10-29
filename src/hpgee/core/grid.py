from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Any

from hpgee.core.models import Geometry, PatchGrid

AFFINE_KEYS = ("scaleX", "shearX", "translateX", "shearY", "scaleY", "translateY")


@dataclass(frozen=True)
class PixelGrid:
    """Exact pixel support shared by sampling, computation and delivery.

    Affine values retain their precision. Domain requirements such as 30 m UTM
    or square patches belong to their callers, not this shared representation.
    """
    crs: str
    transform: tuple[float, ...]
    width: int
    height: int
    crs_field: str = "crsCode"

    def __post_init__(self):
        if not isinstance(self.crs, str) or not self.crs.strip():
            raise ValueError("An explicit pixel-grid CRS is required")
        if self.crs_field not in ("crsCode", "crsWkt"):
            raise ValueError("Unsupported CRS representation")
        object.__setattr__(self, "transform", tuple(self.transform))
        if len(self.transform) != 6 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in self.transform):
            raise ValueError("Pixel grid requires six finite affine coefficients")
        a, b, _, d, e, _ = self.transform
        if a * e - b * d == 0:
            raise ValueError("Pixel-grid affine transform must be invertible")
        if any(type(n) is not int or n <= 0 for n in (self.width, self.height)):
            raise ValueError("Pixel-grid dimensions must be positive integers")

    @classmethod
    def from_ee(cls, grid):
        field = "crsCode" if grid.get("crsCode") else "crsWkt"
        affine = grid["affineTransform"]
        return cls(grid[field], tuple(affine.get(k, 0) for k in AFFINE_KEYS),
                   grid["dimensions"]["width"], grid["dimensions"]["height"], field)

    @classmethod
    def from_config(cls, grid):
        return cls(grid["crs"], tuple(grid["transform"]), grid["width"], grid["height"],
                   grid.get("crs_field", "crsCode"))

    def to_ee(self):
        return {"dimensions": {"width": self.width, "height": self.height},
                "affineTransform": dict(zip(AFFINE_KEYS, self.transform)),
                self.crs_field: self.crs}

    def spatial_spec(self):
        return {"crs": self.crs, "transform": list(self.transform)}

    def to_config(self):
        result = {**self.spatial_spec(), "width": self.width, "height": self.height}
        if self.crs_field != "crsCode":
            result["crs_field"] = self.crs_field
        return result


def representative_lon_lat(geometry: Geometry) -> tuple[float, float]:
    if geometry["type"] == "Point":
        lon, lat = geometry["coordinates"][:2]
        return float(lon), float(lat)
    try:
        from shapely.geometry import shape
    except ImportError as exc:  # pragma: no cover - dependency error is explicit
        raise ImportError("Polygon inputs require the 'geo' extra (shapely)") from exc
    point = shape(geometry).representative_point()
    return float(point.x), float(point.y)


def utm_epsg(lon: float, lat: float) -> str:
    zone = min(60, max(1, int((lon + 180.0) // 6.0) + 1))
    return f"EPSG:{32600 + zone if lat >= 0 else 32700 + zone}"


@lru_cache(maxsize=128)
def _transformer(epsg: str):
    try:
        from pyproj import Transformer
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Patch downloads require pyproj") from exc
    return Transformer.from_crs("EPSG:4326", epsg, always_xy=True)


@dataclass(frozen=True)
class ComputedGrid:
    crs: str
    width: int
    height: int
    scale_x: float
    scale_y: float
    translate_x: float
    translate_y: float

    def to_ee(self) -> dict[str, Any]:
        return PixelGrid(self.crs,
                         (self.scale_x, 0, self.translate_x, 0, self.scale_y, self.translate_y),
                         self.width, self.height).to_ee()


def compute_grid(geometry: Mapping[str, Any], spec: PatchGrid) -> ComputedGrid:
    lon, lat = representative_lon_lat(geometry)
    crs = utm_epsg(lon, lat)
    center_x, center_y = _transformer(crs).transform(lon, lat)
    width_m = spec.size * spec.scale
    return ComputedGrid(
        crs=crs,
        width=spec.size,
        height=spec.size,
        scale_x=spec.scale,
        scale_y=-spec.scale,
        translate_x=center_x - width_m / 2.0,
        translate_y=center_y + width_m / 2.0,
    )
