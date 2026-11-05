from __future__ import annotations

import sqlite3

import mapbox_vector_tile
import pytest

from offline_railway_map.coverage import CellCoverage
from offline_railway_map.grid import lonlat_to_tile
from offline_railway_map.processing import build_cell
from offline_railway_map.rail_tiles import RailTiles
from railfixtures import CELL, elements

CX, CY = CELL


def tile_at(z, lon, lat):
    x, y = lonlat_to_tile(lon, lat, z)
    return int(x), int(y)


@pytest.fixture
def built(home):
    path = home.cell_dir("210-97") / "rail.sqlite"
    result = build_cell(elements(), CX, CY, path)
    return home, path, result


def decode(path, z, x, y):
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT data FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)).fetchone()
    conn.close()
    return mapbox_vector_tile.decode(row[0]) if row else {}


def ids(layer):
    return sorted({f["id"] for f in layer["features"]})


def test_cell_database_contents(built):
    _home, path, result = built
    assert result.lines == 6  # the platform is not a drawn line
    assert result.ways == 6
    names = sorted(row[3] for row in result.stations)
    assert names == ["小站", "测试站", "面状车站"], "stations outside the cell are dropped"
    conn = sqlite3.connect(path)
    tags = dict(conn.execute("SELECT id, tags FROM ways").fetchall())
    assert '"京沪高铁"' in tags[1]
    lod_ids = sorted({r[0] for r in conn.execute("SELECT id FROM lod")})
    assert lod_ids == [1, 2, 4, 5], "simplified copies only for lines shown at every zoom"
    conn.close()


def test_zoom_rules_in_tiles(built):
    _home, path, _ = built
    z8 = decode(path, 8, CX, CY)
    assert ids(z8["rail"]) == [1, 2, 4, 5]
    assert "stations" not in z8
    assert all("lb" not in f["properties"] for f in z8["rail"]["features"]), "no numbers below z9"

    x9, y9 = tile_at(9, 116.0, 39.4)
    z9 = decode(path, 9, x9, y9)
    labels = {f["id"]: f["properties"].get("lb") for f in z9["rail"]["features"]}
    assert labels[1] == "350"
    assert [f["properties"]["n"] for f in z9["stations"]["features"]] == ["测试站"]

    x10, y10 = tile_at(10, 116.01, 39.305)
    assert 3 not in ids(decode(path, 10, x10, y10)["rail"]), "sidings start at z11"
    x11, y11 = tile_at(11, 116.01, 39.305)
    z11 = decode(path, 11, x11, y11)
    assert 3 in ids(z11["rail"])

    x12, y12 = tile_at(12, 116.05, 39.45)
    halt = decode(path, 12, x12, y12)["stations"]["features"]
    assert [f["properties"]["k"] for f in halt] == ["halt"]


def test_line_properties(built):
    _home, path, _ = built
    x9, y9 = tile_at(9, 116.2, 39.15)
    feats = {f["id"]: f["properties"] for f in decode(path, 9, x9, y9)["rail"]["features"]}
    branch = feats[2]
    assert branch["sp"] == 120 and branch["lb"] == "120/80" and branch["lc"] == "line"
    z8 = {f["id"]: f["properties"] for f in decode(path, 8, CX, CY)["rail"]["features"]}
    assert z8[1]["sp"] == 350 and z8[1]["lc"] == "main" and z8[1]["w"] == 3.5
    assert z8[4]["st"] == "p" and z8[4]["sp"] == 250
    assert z8[5]["st"] == "c" and "sp" not in z8[5]
    assert z8[1]["sk"] > z8[2]["sk"] > z8[5]["sk"], "main line drawn above branch above construction"


def test_lines_are_clipped_to_the_cell(built):
    _home, path, _ = built
    z8 = decode(path, 8, CX, CY)
    main = next(f for f in z8["rail"]["features"] if f["id"] == 1)
    xs = [pt[0] for pt in main["geometry"]["coordinates"]]
    assert min(xs) >= -1, "the part west of the cell is cut off at the cell border"


def test_rail_tiles_service(built):
    home, _path, _ = built
    coverage = CellCoverage({CELL})
    tiles = RailTiles(home, coverage)
    tiles.set_version("210-97", 1)
    assert tiles.tile(8, CX, CY)
    assert tiles.tile(8, CX + 1, CY) == b"", "cells that are not cached have no tiles"
    low = mapbox_vector_tile.decode(tiles.tile(5, CX >> 3, CY >> 3))
    assert ids(low["rail"]) == [1, 2, 4, 5]
    assert tiles.tile(5, CX >> 3, CY >> 3) == tiles.tile(5, CX >> 3, CY >> 3)
    assert tiles.way_tags("210-97", 1)["maxspeed"] == "350"
    assert (home.cache_dir / "rail_lowzoom.sqlite").exists()
    tiles.close_all()


def test_empty_cell(home):
    result = build_cell([], 1, 1, home.cell_dir("1-1") / "rail.sqlite")
    assert (result.lines, result.tiles) == (0, 0)
