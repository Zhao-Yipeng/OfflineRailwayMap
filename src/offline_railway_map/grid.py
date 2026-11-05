"""The cell grid: one cell is one z8 Web Mercator tile (SPEC §5)."""

from __future__ import annotations

import math
import re

CELL_ZOOM = 8
CELLS_PER_SIDE = 1 << CELL_ZOOM  # 256
MAX_LAT = 85.0511287798066

_ID = re.compile(r"^(\d{1,3})-(\d{1,3})$")


def cell_id(x: int, y: int) -> str:
    return f"{x}-{y}"


def parse_cell_id(value: str) -> tuple[int, int]:
    match = _ID.match(str(value))
    if not match:
        raise ValueError(f"bad cell id: {value!r}")
    x, y = int(match.group(1)), int(match.group(2))
    if not (0 <= x < CELLS_PER_SIDE and 0 <= y < CELLS_PER_SIDE):
        raise ValueError(f"cell out of range: {value!r}")
    return x, y


def tile_lat(z: int, y: float) -> float:
    n = 1 << z
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) in degrees."""
    n = 1 << z
    return x / n * 360 - 180, tile_lat(z, y + 1), (x + 1) / n * 360 - 180, tile_lat(z, y)


def cell_bounds(x: int, y: int) -> tuple[float, float, float, float]:
    return tile_bounds(CELL_ZOOM, x, y)


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[float, float]:
    """Fractional tile coordinates of a point."""
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    n = 1 << z
    x = (lon + 180) / 360 * n
    rad = math.radians(lat)
    y = (1 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi) / 2 * n
    return x, y


def cell_at(lon: float, lat: float) -> tuple[int, int]:
    x, y = lonlat_to_tile(lon, lat, CELL_ZOOM)
    return min(CELLS_PER_SIDE - 1, max(0, int(x))), min(CELLS_PER_SIDE - 1, max(0, int(y)))


def cell_center(x: int, y: int) -> tuple[float, float]:
    west, south, east, north = cell_bounds(x, y)
    return (west + east) / 2, tile_lat(CELL_ZOOM, y + 0.5)


def distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))


def cell_size_km(y: int) -> float:
    """Approximate side length of the cells in row y (SPEC §5.1)."""
    return 40075.016686 / CELLS_PER_SIDE * math.cos(math.radians(tile_lat(CELL_ZOOM, y + 0.5)))
