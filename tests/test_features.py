"""Search, details, settings, size estimate and clearing the cache (SPEC §6.6–6.10, §9.7)."""

from __future__ import annotations

import json

import pytest

import offline_railway_map.search as search_module
from fakeserver import FakeServer
from offline_railway_map.processing import build_cell
from railfixtures import CELL, elements
from test_downloader import make_core, wait_for

ORM_REPLY = [{"name": "北京南", "railway": "station", "operator": "中国铁路", "latitude": 39.865, "longitude": 116.379}]
NOMINATIM_REPLY = [{"display_name": "北京市, 中国", "type": "city", "lat": "39.9", "lon": "116.4",
                    "boundingbox": ["39.4", "41.1", "115.4", "117.5"]}]


@pytest.fixture
def services(monkeypatch):
    server = FakeServer().start()
    server.routes["/orm"] = lambda r: (200, {"Content-Type": "application/json"}, json.dumps(ORM_REPLY).encode())
    server.routes["/nominatim"] = lambda r: (200, {"Content-Type": "application/json"}, json.dumps(NOMINATIM_REPLY).encode())
    monkeypatch.setattr(search_module, "ORM_API", server.url + "/orm")
    monkeypatch.setattr(search_module, "NOMINATIM", server.url + "/nominatim")
    yield server
    server.stop()


def install_fixture_cell(core):
    """Put the synthetic cell 210-97 in place as if it had been downloaded."""
    result = build_cell(elements(), *CELL, core.home.cell_dir("210-97") / "rail.sqlite")
    core.cells.apply(["210-97"], [], None)
    core.store.update_cell("210-97", status="cached", rail_at=1_700_000_000.0, rail_bytes=result.size)
    core.store.replace_stations("210-97", result.stations)
    core.rail.set_version("210-97", 1)
    core.cells.sync_coverage()


def test_search_all_sources(client, core, services):
    install_fixture_cell(core)
    status, result = client.get_json("/api/search?q=%E6%B5%8B%E8%AF%95")  # 测试
    assert status == 200
    assert [s["name"] for s in result["local"]] == ["测试站"]
    assert result["orm"][0]["name"] == "北京南" and result["orm"][0]["lon"] == 116.379
    assert result["places"][0]["bbox"] == [115.4, 39.4, 117.5, 41.1]
    ua = {r.headers["User-Agent"] for r in services.requests}
    assert len(ua) == 1 and ua.pop().startswith("OfflineRailwayMap/")
    client.get_json("/api/search?q=%E6%B5%8B%E8%AF%95")
    assert len(services.requests) == 2, "repeated queries are answered from the cache"


def test_search_offline_uses_local_stations_only(client, core, services):
    install_fixture_cell(core)
    for _ in range(3):
        core.status.network_down()
    status, result = client.get_json("/api/search?q=%E5%B0%8F%E7%AB%99")  # 小站
    assert result["offline"] is True
    assert [s["name"] for s in result["local"]] == ["小站"]
    assert result["orm"] is None and result["places"] is None
    assert services.requests == []


def test_local_search_lists_a_station_mapped_twice_once(home):
    from offline_railway_map.store import StateStore

    store = StateStore(home)
    store.replace_stations("210-97", [
        ("node", 1, "urban", "北京南站", 116.3790, 39.8650),  # metro line 4
        ("node", 2, "urban", "北京南站", 116.3800, 39.8640),  # metro line 14, 140 m away
        ("node", 3, "station", "北京南", 116.3785, 39.8652),
        ("node", 4, "station", "北京南站", 116.3790, 39.8650),  # other kind: kept
        ("node", 5, "urban", "北京南站", 117.2000, 39.1000),  # same name far away: kept
    ])
    store.replace_stations("210-96", [("node", 3, "station", "北京南", 116.3785, 39.8652)])  # on the border
    found = [(s["osmId"], s["name"], s["kind"]) for s in store.search_stations("北京南")]
    assert found == [(3, "北京南", "station"), (1, "北京南站", "urban"), (4, "北京南站", "station"),
                     (5, "北京南站", "urban")]
    assert len(store.search_stations("北京南", limit=2)) == 2
    store.close()


def test_orm_rate_limit_pauses_the_api(client, services):
    services.routes["/orm"] = lambda r: (429, {}, b"")
    status, result = client.get_json("/api/search?q=abc")
    assert "orm" in result["errors"]
    client.get_json("/api/search?q=abcd")
    assert sum(1 for r in services.requests if r.path.startswith("/orm")) == 1


def test_details_of_a_cached_line(client, core):
    install_fixture_cell(core)
    status, info = client.get_json("/api/details?cell=210-97&way=2")
    assert status == 200
    assert info["speedLabel"] == "120/80" and info["colorSpeed"] == 120
    assert info["usage"] == "branch" and info["state"] == "operating"
    assert info["railAt"] == 1_700_000_000.0
    assert client.get_json("/api/details?cell=210-97&way=999")[0] == 404
    assert client.get_json("/api/details?cell=bad&way=1")[0] == 400


def test_settings_api(client, core, home):
    status, data = client.get_json("/api/settings")
    assert data["settings"]["basemapMaxzoom"] == 14 and "GPL-3.0" in data["about"]["license"]
    assert not home.data_exists()
    status, _ = client.post_json("/api/settings", {"basemapMaxzoom": 99})
    assert status == 400 and not home.data_exists()
    status, data = client.post_json("/api/settings", {"basemapMaxzoom": 13, "showCachedOutline": False})
    assert status == 200 and data["settings"]["basemapMaxzoom"] == 13
    assert "basemap_maxzoom = 13" in home.settings_file.read_text(encoding="utf-8")


def test_clear_cache_removes_everything_and_resets(home, hooks):
    server = FakeServer().start()
    from railfixtures import overpass_json
    from pmtiles_writer import range_route, write_archive
    from test_basemap import cell_tiles

    server.routes["/api/interpreter"] = lambda r: (200, {"Content-Type": "application/json"}, overpass_json())
    server.routes["/basemap.pmtiles"] = range_route(write_archive(cell_tiles(max_zoom=9), max_zoom=9))
    core = make_core(home, hooks, server.url + "/api/interpreter")
    try:
        core.settings.show_cached_outline = False
        core.settings.save(home)
        core.cells.apply(["210-97"], [], None)
        assert wait_for(lambda: core.store.cell("210-97")["status"] == "cached")
        core.online.cache.put("orm", 1, 0, 0, __import__("offline_railway_map.online_tiles", fromlist=["CachedTile"]).CachedTile(b"x", None, None, 0))
        assert core.rail.tile(5, 210 >> 3, 97 >> 3)  # creates the low-zoom disk cache
        assert core.estimate(["211-97"])["bytes"] > 0

        assert core.clear_cache() == []
        assert not home.data.exists(), "the program folder is back to its unpacked state"
        assert core.cells.snapshot()["cells"] == []
        assert not core.coverage.cells()
        assert core.settings.show_cached_outline is True, "settings are back to defaults"
        assert core.rail.tile(8, 210, 97) == b""
        assert core.basemap_tiles.tile(4, 210 >> 4, 97 >> 4) is None

        # the program keeps working afterwards
        core.settings.overpass_endpoints = [server.url + "/api/interpreter"]
        core.settings.basemap_source = server.url + "/basemap.pmtiles"
        core.cells.apply(["210-97"], [], None)
        assert wait_for(lambda: (core.store.cell("210-97") or {}).get("status") == "cached")
    finally:
        core.shutdown()
        server.stop()
