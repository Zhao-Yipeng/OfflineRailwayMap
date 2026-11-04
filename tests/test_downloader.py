"""End-to-end download of one cell against a fake Overpass server."""

from __future__ import annotations

import time

import pytest

from fakeserver import FakeServer
from offline_railway_map.core import AppCore
from offline_railway_map.settings import Settings
from pmtiles_writer import range_route, write_archive
from railfixtures import overpass_json
from test_basemap import cell_tiles


@pytest.fixture
def overpass():
    """Fake Overpass server that also serves a small basemap archive."""
    server = FakeServer().start()
    server.routes["/basemap.pmtiles"] = range_route(write_archive(cell_tiles(max_zoom=9), max_zoom=9))
    yield server
    server.stop()


def make_core(home, hooks, endpoint):
    settings = Settings()
    settings.overpass_endpoints = [endpoint]
    settings.basemap_source = endpoint.rsplit("/api/", 1)[0] + "/basemap.pmtiles"
    core = AppCore(home, settings, hooks=hooks)
    core.overpass.min_interval = 0
    core.start()
    return core


def wait_for(predicate, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_cell_is_downloaded_processed_and_installed(home, hooks, overpass):
    overpass.routes["/api/interpreter"] = lambda r: (200, {"Content-Type": "application/json"}, overpass_json())
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    try:
        core.cells.apply(["210-97"], [], (116, 39))
        assert wait_for(lambda: core.store.cell("210-97")["status"] == "cached"), core.store.cell("210-97")
        row = core.store.cell("210-97")
        assert row["rail_bytes"] > 0 and row["rail_at"] and row["error"] is None
        assert core.coverage.is_cached(210, 97)
        assert (home.cell_dir("210-97") / "rail.sqlite").exists()
        assert not (home.tmp_dir / "210-97").exists(), "work folder is removed after install"
        assert core.rail.tile(8, 210, 97)
        assert [s["name"] for s in core.store.search_stations("测试")] == ["测试站"]
        # offline basemap: cell tiles in the cell folder, ancestors in the shared store
        assert row["basemap_maxzoom"] == 9 and row["basemap_bytes"] > 0
        assert (home.cell_dir("210-97") / "basemap.sqlite").exists()
        assert core.basemap_tiles.tile(9, 420, 195) is not None
        assert core.basemap_tiles.tile(4, 210 >> 4, 97 >> 4) is not None
        assert core.cells.snapshot()["basemapMaxzoom"] == 9
        # update the railway data: the cell stays cached the whole time
        version = core.cells.tiles_version
        assert core.cells.request_update("210-97", with_basemap=False)
        assert core.coverage.is_cached(210, 97)
        assert wait_for(lambda: core.store.cell("210-97")["status"] == "cached" and core.cells.tiles_version > version)
        assert sum(1 for r in overpass.requests if r.path.startswith("/api/")) == 2
        assert (home.cell_dir("210-97") / "basemap.sqlite").exists(), "a railway-only update keeps the basemap"
        core.cells.delete(["210-97"])
        assert core.basemap_tiles.tile(4, 210 >> 4, 97 >> 4) is None, "ancestors go with the last cell"
    finally:
        core.shutdown()


def test_rejected_query_marks_cell_failed(home, hooks, overpass):
    overpass.routes["/api/interpreter"] = lambda r: (400, {}, b"bad query")
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    try:
        core.cells.apply(["210-97"], [], None)
        assert wait_for(lambda: core.store.cell("210-97")["status"] == "failed")
        assert "rejected" in core.store.cell("210-97")["error"]
        assert not core.coverage.is_cached(210, 97)
    finally:
        core.shutdown()


def test_deleting_a_cell_during_download_cancels_it(home, hooks, overpass):
    def slow(rec):
        time.sleep(1.0)
        return 200, {"Content-Type": "application/json"}, overpass_json()

    overpass.routes["/api/interpreter"] = slow
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    try:
        core.cells.apply(["210-97"], [], None)
        assert wait_for(lambda: core.store.cell("210-97")["status"] == "downloading")
        core.cells.delete(["210-97"])
        time.sleep(1.5)
        assert core.store.cell("210-97") is None
        assert not home.cell_dir("210-97").exists()
        assert not core.coverage.is_cached(210, 97)
    finally:
        core.shutdown()
