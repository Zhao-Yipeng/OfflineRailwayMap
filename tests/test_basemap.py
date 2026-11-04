from __future__ import annotations

import gzip
import random
import sqlite3
import threading

import pytest
import requests

from fakeserver import FakeServer
from offline_railway_map.basemap import (
    BasemapFetcher,
    BasemapTiles,
    BuildSelector,
    LowzoomStore,
    install_ancestors,
    plan_cell,
)
from offline_railway_map.coverage import CellCoverage
from offline_railway_map.pmtiles import RemoteArchive, descendant_range, id_to_zxy, zxy_to_id
from offline_railway_map.rail_tiles import RailTiles
from pmtiles_writer import range_route, write_archive

CX, CY = 210, 97


def test_tile_id_round_trip():
    assert [zxy_to_id(0, 0, 0), zxy_to_id(1, 0, 0), zxy_to_id(1, 0, 1), zxy_to_id(1, 1, 1), zxy_to_id(1, 1, 0)] == [0, 1, 2, 3, 4]
    rng = random.Random(1)
    for _ in range(500):
        z = rng.randint(0, 16)
        x, y = rng.randrange(1 << z), rng.randrange(1 << z)
        assert id_to_zxy(zxy_to_id(z, x, y)) == (z, x, y)


def test_descendants_form_one_id_range():
    for z in (9, 11, 13):
        lo, hi = descendant_range(8, CX, CY, z)
        k = z - 8
        ids = {zxy_to_id(z, (CX << k) + i, (CY << k) + j) for i in range(1 << k) for j in range(1 << k)}
        assert ids == set(range(lo, hi))


def cell_tiles(max_zoom=10):
    """Fake basemap: every tile of the cell up to max_zoom, plus ancestors and a neighbour."""
    tiles = {}
    for z in range(8, max_zoom + 1):
        k = z - 8
        for i in range(1 << k):
            for j in range(1 << k):
                tiles[(z, (CX << k) + i, (CY << k) + j)] = gzip.compress(f"{z}/{i}/{j}".encode())
    for z in range(8):
        tiles[(z, CX >> (8 - z), CY >> (8 - z))] = gzip.compress(f"ancestor {z}".encode())
    tiles[(8, CX + 1, CY)] = gzip.compress(b"neighbour")
    return tiles


@pytest.fixture
def archive_server():
    server = FakeServer().start()
    server.routes["/flat.pmtiles"] = range_route(write_archive(cell_tiles()))
    server.routes["/leaves.pmtiles"] = range_route(write_archive(cell_tiles(), leaf_size=7))
    yield server
    server.stop()


@pytest.mark.parametrize("name", ["flat.pmtiles", "leaves.pmtiles"])
def test_plan_finds_exactly_the_cell(archive_server, name):
    archive = RemoteArchive(requests.Session(), f"{archive_server.url}/{name}", retry_delays=())
    tiles, ancestors = plan_cell(archive, CX, CY, 10)
    assert len(tiles) == 1 + 4 + 16
    assert all(z >= 8 and (x >> (z - 8), y >> (z - 8)) == (CX, CY) for z, x, y, _o, _n in tiles)
    assert sorted(a[0] for a in ancestors) == list(range(8))


def test_extract_cell_and_share_ancestors(home, archive_server):
    url = f"{archive_server.url}/leaves.pmtiles"
    fetcher = BasemapFetcher(requests.Session(), BuildSelector(requests.Session(), lambda: url), lambda: 9)
    work = home.ensure_dir(home.tmp_dir / "210-97")
    progress = []
    info = fetcher.fetch(CX, CY, work, threading.Event(), progress.append)
    assert info["basemap_build"] == url and info["basemap_maxzoom"] == 9 and info["basemap_bytes"] > 0
    assert progress[-1] == 1.0
    conn = sqlite3.connect(work / "basemap.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0] == 1 + 4
    assert conn.execute("SELECT MAX(z) FROM tiles").fetchone()[0] == 9
    conn.close()
    estimate = fetcher.estimate(CX, CY)
    assert estimate > 0

    # install into the cell folder and the shared low-zoom store
    cell_dir = home.cell_dir("210-97")
    cell_dir.parent.mkdir(parents=True, exist_ok=True)
    work.rename(cell_dir)
    lowzoom = LowzoomStore(home)
    install_ancestors(cell_dir, "210-97", lowzoom)
    assert lowzoom.get(5, CX >> 3, CY >> 3)[0] == gzip.compress(b"ancestor 5")
    tiles = BasemapTiles(home, CellCoverage({(CX, CY)}), lowzoom, RailTiles(home, CellCoverage({(CX, CY)})))
    data, compression = tiles.tile(9, CX * 2 + 1, CY * 2)
    assert gzip.decompress(data) == b"9/1/0" and compression == 2
    assert tiles.tile(8, CX + 1, CY) is None, "neighbouring cells are not cached"
    assert tiles.tile(3, CX >> 5, CY >> 5) is not None

    # a second cell sharing the ancestors keeps them alive until both are gone
    lowzoom.add("211-97", [(7, CX >> 1, CY >> 1, b"x")], 2)
    lowzoom.remove("210-97")
    assert lowzoom.get(7, CX >> 1, CY >> 1) is not None
    assert lowzoom.get(5, CX >> 3, CY >> 3) is None
    lowzoom.remove("211-97")
    assert lowzoom.get(7, CX >> 1, CY >> 1) is None
    lowzoom.close()


def test_build_selector_picks_newest_v4(home):
    server = FakeServer().start()
    server.routes["/builds.json"] = lambda r: (200, {"Content-Type": "application/json"}, (
        b'[{"key":"20250101.pmtiles","version":"4.1.0"},{"key":"20261103.pmtiles","version":"4.13.6"},'
        b'{"key":"20261104.pmtiles","version":"5.0.0"},{"key":"20240101.pmtiles","version":"3.0.0"}]'
    ))
    try:
        import offline_railway_map.basemap as basemap_module
        selector = BuildSelector(requests.Session(), lambda: "auto")
        old = basemap_module.BUILDS_URL
        basemap_module.BUILDS_URL = server.url + "/builds.json"
        try:
            key, url = selector.current()
        finally:
            basemap_module.BUILDS_URL = old
        assert key == "20261103.pmtiles"
        assert url == "https://build.protomaps.com/20261103.pmtiles"
        assert BuildSelector(requests.Session(), lambda: "https://example.org/a.pmtiles").current()[1] == "https://example.org/a.pmtiles"
    finally:
        server.stop()
