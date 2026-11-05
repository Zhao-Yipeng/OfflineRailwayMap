"""Turn downloaded OSM elements into a cell's ``rail.sqlite`` (SPEC §9.4).

* every line is clipped to the cell rectangle;
* the speed colour / label, start zoom, width class and draw order are
  computed once (SPEC §7.3–7.5, §8);
* vector tiles z8–z14 are generated up front (empty tiles are not stored);
* lines shown at every zoom get two simplified copies for z2–z5 and z6–z7;
* stations and the full tags of every way are kept for search and details.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import mapbox_vector_tile
import numpy as np
import shapely
from shapely.geometry import LineString, MultiLineString, Point, box

from .classify import STATE_CODES, STATION_KINDS, classify, station_kind
from .grid import CELL_ZOOM, MAX_LAT, cell_bounds
from .speed import color_speed, speed_label

EXTENT = 4096
BUFFER = 64 / EXTENT  # tile buffer, as a fraction of the tile size
MAX_TILE_ZOOM = 14
EARTH_RADIUS = 6378137.0
ORIGIN = np.pi * EARTH_RADIUS  # 20037508.34 m
LOD_LEVELS = (5, 7)  # simplified geometry for z2–5 and z6–7

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE ways (id INTEGER PRIMARY KEY, tags TEXT NOT NULL);
CREATE TABLE tiles (z INTEGER, x INTEGER, y INTEGER, data BLOB NOT NULL, PRIMARY KEY (z, x, y));
CREATE TABLE lod (level INTEGER NOT NULL, id INTEGER NOT NULL, props TEXT NOT NULL, geom BLOB NOT NULL);
CREATE INDEX lod_level ON lod(level);
CREATE TABLE stations (osm_type TEXT, osm_id INTEGER, kind TEXT, name TEXT, lon REAL, lat REAL);
"""


class Cancelled(Exception):
    pass


def to_mercator(coords: np.ndarray) -> np.ndarray:
    lon = coords[:, 0]
    lat = np.clip(coords[:, 1], -MAX_LAT, MAX_LAT)
    x = np.radians(lon) * EARTH_RADIUS
    y = np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * EARTH_RADIUS
    return np.column_stack([x, y])


def tile_bounds_m(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Tile bounds in Web Mercator metres: (minx, miny, maxx, maxy)."""
    size = 2 * ORIGIN / (1 << z)
    minx = -ORIGIN + x * size
    maxy = ORIGIN - y * size
    return minx, maxy - size, minx + size, maxy


def _lines(geom) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom] if geom.length > 0 else []
    if hasattr(geom, "geoms"):
        return [g for part in geom.geoms for g in _lines(part)]
    return []


def _merge(parts: list[LineString]):
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiLineString(parts)


def _layer(tags: dict) -> int:
    try:
        return max(-5, min(5, int(tags.get("layer", 0))))
    except (TypeError, ValueError):
        return 0


@dataclass
class _Line:
    way_id: int
    geom: object  # metres
    props: dict
    minzoom: int
    labelzoom: int | None
    label: str | None


@dataclass
class _Station:
    point: Point  # metres
    props: dict
    minzoom: int


@dataclass
class CellResult:
    ways: int
    lines: int
    tiles: int
    stations: list[tuple[str, int, str, str, float, float]]
    size: int


def build_cell(
    elements: Iterable[dict],
    cx: int,
    cy: int,
    out_path: Path,
    progress: Callable[[float], None] = lambda _p: None,
    cancel: threading.Event | None = None,
    meta: dict | None = None,
) -> CellResult:
    west, south, east, north = cell_bounds(cx, cy)
    lines: list[_Line] = []
    ways: dict[int, str] = {}
    stations: list[_Station] = []
    station_rows: list[tuple[str, int, str, str, float, float]] = []

    for element in elements:
        tags = element.get("tags") or {}
        if element.get("type") == "way" and (element.get("geometry") or element.get("coords") is not None):
            cls = classify(tags)
            if cls is None:
                continue
            # Overpass: geometry [{lat, lon}, ...]; regional extracts: coords (lon, lat) array
            coords = element.get("coords")
            if coords is None:
                coords = [(p["lon"], p["lat"]) for p in element["geometry"] if p]
            if len(coords) < 2:
                continue
            clipped = _merge(_lines(shapely.clip_by_rect(LineString(coords), west, south, east, north)))
            if clipped is None:
                continue
            geom = shapely.transform(clipped, to_mercator)
            speed = color_speed(tags)
            props = {
                "st": STATE_CODES[cls.state],
                "w": cls.width,
                "lc": cls.low_class,
                "sk": (_layer(tags) + 10) * 10_000_000 + cls.rank * 1000 + (speed + 1 if speed is not None else 0),
                "lp": cls.rank * 1000 + (speed or 0),
            }
            if speed is not None:
                props["sp"] = speed
            lines.append(_Line(element["id"], geom, props, cls.minzoom, cls.labelzoom, speed_label(tags)))
            ways[element["id"]] = json.dumps(tags, ensure_ascii=False, separators=(",", ":"))
            continue
        kind = station_kind(tags)
        if kind is None:
            continue
        where = element if "lat" in element else element.get("center")
        if not where:
            continue
        lon, lat = where["lon"], where["lat"]
        if not (west <= lon < east and south <= lat < north):
            continue
        name = tags.get("name", "")
        (mx, my), = to_mercator(np.array([[lon, lat]]))
        stations.append(_Station(Point(mx, my), {"k": kind, "n": name}, STATION_KINDS[kind]))
        if name:
            station_rows.append((element["type"], element["id"], kind, name, lon, lat))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(out_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT OR REPLACE INTO ways VALUES (?, ?)", ways.items())
        conn.executemany(
            "INSERT INTO stations VALUES (?, ?, ?, ?, ?, ?)", station_rows
        )
        tiles = _write_tiles(conn, lines, stations, cx, cy, progress, cancel)
        _write_lod(conn, lines)
        for key, value in (meta or {}).items():
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))
        conn.commit()
    finally:
        conn.close()
    return CellResult(len(ways), len(lines), tiles, station_rows, out_path.stat().st_size)


def _write_tiles(conn, lines, stations, cx, cy, progress, cancel) -> int:
    if not lines and not stations:
        progress(1.0)
        return 0
    tree = shapely.STRtree([line.geom for line in lines]) if lines else None
    station_tree = shapely.STRtree([s.point for s in stations]) if stations else None
    written = 0
    zooms = range(CELL_ZOOM, MAX_TILE_ZOOM + 1)
    total = sum(4 ** (z - CELL_ZOOM) for z in zooms)
    done = 0
    for z in zooms:
        n = 1 << (z - CELL_ZOOM)
        for ty in range(cy * n, cy * n + n):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            for tx in range(cx * n, cx * n + n):
                done += 1
                data = _encode_tile(z, tx, ty, lines, tree, stations, station_tree)
                if data:
                    conn.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (z, tx, ty, data))
                    written += 1
            progress(done / total)
    return written


def _encode_tile(z, tx, ty, lines, tree, stations, station_tree) -> bytes | None:
    bounds = tile_bounds_m(z, tx, ty)
    minx, miny, maxx, maxy = bounds
    pad = (maxx - minx) * BUFFER
    padded = (minx - pad, miny - pad, maxx + pad, maxy + pad)
    tolerance = (maxx - minx) / EXTENT
    features = []
    if tree is not None:
        for index in tree.query(box(*padded)):
            line = lines[index]
            if line.minzoom > z:
                continue
            geom = _merge(_lines(shapely.clip_by_rect(line.geom, *padded)))
            if geom is None:
                continue
            geom = shapely.simplify(geom, tolerance, preserve_topology=False)
            if geom.is_empty:
                continue
            props = dict(line.props)
            if line.label and line.labelzoom is not None and z >= line.labelzoom:
                props["lb"] = line.label
            features.append({"geometry": geom, "properties": props, "id": line.way_id})
    points = []
    if station_tree is not None:
        for index in station_tree.query(box(*bounds)):
            station = stations[index]
            p = station.point
            if station.minzoom <= z and minx <= p.x < maxx and miny < p.y <= maxy:
                points.append({"geometry": p, "properties": station.props})
    if not features and not points:
        return None
    layers = []
    if features:
        layers.append({"name": "rail", "features": features})
    if points:
        layers.append({"name": "stations", "features": points})
    return mapbox_vector_tile.encode(layers, default_options={"quantize_bounds": bounds, "extents": EXTENT})


def lod_tolerance(level: int) -> float:
    """About half a pixel of a 256 px tile at zoom ``level``."""
    return 2 * ORIGIN / (1 << level) / 512


def _write_lod(conn, lines) -> None:
    rows = []
    for line in lines:
        if line.minzoom != 0:
            continue
        for level in LOD_LEVELS:
            geom = shapely.simplify(line.geom, lod_tolerance(level), preserve_topology=False)
            if geom.is_empty or geom.length == 0:
                continue
            rows.append((level, line.way_id, json.dumps(line.props, separators=(",", ":")), shapely.to_wkb(geom)))
    conn.executemany("INSERT INTO lod VALUES (?, ?, ?, ?)", rows)


def encode_lowzoom(z: int, x: int, y: int, features: list[tuple[int, dict, object]]) -> bytes:
    """Encode a z2–z7 tile from (way id, props, geometry in metres) tuples."""
    bounds = tile_bounds_m(z, x, y)
    minx, miny, maxx, maxy = bounds
    pad = (maxx - minx) * BUFFER
    padded = (minx - pad, miny - pad, maxx + pad, maxy + pad)
    out = []
    for way_id, props, geom in features:
        clipped = _merge(_lines(shapely.clip_by_rect(geom, *padded)))
        if clipped is not None:
            out.append({"geometry": clipped, "properties": props, "id": way_id})
    if not out:
        return b""
    return mapbox_vector_tile.encode([{"name": "rail", "features": out}], default_options={"quantize_bounds": bounds, "extents": EXTENT})
