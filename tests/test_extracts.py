"""Railway data from regional extracts (SPEC §9.8)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import osmium
import pytest
from osmium.osm.mutable import Node, Way

from fakeserver import FakeServer
from offline_railway_map import extracts
from offline_railway_map.extracts import (
    ExtractStore,
    ExtractUnavailable,
    RegionIndex,
    filter_railways,
    plan,
    read_cell,
)
from offline_railway_map.grid import cell_bounds, cell_id, tile_bounds
from offline_railway_map.processing import build_cell


def write_pbf(path: Path) -> None:
    """A small extract around Beijing (cell 210-97 and its neighbour 210-96)."""
    with osmium.SimpleWriter(str(path)) as w:
        w.add_node(Node(id=1, location=(116.10, 39.70)))
        w.add_node(Node(id=2, location=(116.30, 39.80)))
        w.add_node(Node(id=3, location=(116.40, 40.10)))  # north of 39.91: cell 210-96
        w.add_node(Node(id=4, location=(116.37, 39.86), tags={"railway": "station", "name": "北京南"}))
        w.add_node(Node(id=5, location=(116.50, 39.70)))
        w.add_node(Node(id=6, location=(116.60, 39.75)))
        w.add_node(Node(id=7, location=(116.30, 39.75)))
        w.add_node(Node(id=8, location=(116.31, 39.75)))
        w.add_node(Node(id=9, location=(116.31, 39.76)))
        w.add_node(Node(id=11, location=(116.20, 39.72)))
        w.add_node(Node(id=12, location=(116.25, 39.73)))
        w.add_way(Way(id=100, nodes=[1, 2, 3], tags={"railway": "rail", "usage": "main", "maxspeed": "350", "name": "京沪高速"}))
        w.add_way(Way(id=101, nodes=[5, 6], tags={"railway": "construction", "construction": "rail", "maxspeed": "250"}))
        w.add_way(Way(id=102, nodes=[11, 12], tags={"highway": "primary"}))  # not a railway
        w.add_way(Way(id=103, nodes=[7, 8, 9, 7], tags={"railway": "station", "name": "丰台"}))  # station area
        w.add_way(Way(id=104, nodes=[11, 999], tags={"railway": "rail"}))  # one node missing: dropped


def square(west, south, east, north) -> dict:
    return {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]]}


def feature(rid, parent, geometry, url):
    return {"type": "Feature", "geometry": geometry,
            "properties": {"id": rid, "parent": parent, "name": rid.title(), "urls": {"pbf": url}}}


def index_json(base: str) -> dict:
    """asia (continent) > china with six provinces; mongolia next to it.

    Cells: 210-96 lies in "beijing", 210-97 in "tianjin"; 211-97 straddles
    "tianjin" and "east1" (lon 117).
    """
    split = 39.9097  # the border between cell rows 96 and 97
    return {"type": "FeatureCollection", "features": [
        feature("asia", None, square(60, -10, 150, 60), base + "/asia.osm.pbf"),
        feature("china", "asia", square(73, 18, 135, 54), base + "/china.osm.pbf"),
        feature("west1", "china", square(73, 18, 94, 54), base + "/west1.osm.pbf"),
        feature("west2", "china", square(94, 18, 115, 54), base + "/west2.osm.pbf"),
        feature("beijing", "china", square(115, split, 117, 54), base + "/beijing.osm.pbf"),
        feature("tianjin", "china", square(115, 18, 117, split), base + "/tianjin.osm.pbf"),
        feature("east1", "china", square(117, 18, 126, 54), base + "/east1.osm.pbf"),
        feature("east2", "china", square(126, 18, 135, 54), base + "/east2.osm.pbf"),
        feature("mongolia", "asia", square(87, 54, 120, 58), base + "/mongolia.osm.pbf"),
    ]}


# -- filtering -----------------------------------------------------------------------------


def test_filter_keeps_railways_and_stations_per_cell(tmp_path):
    pbf = tmp_path / "beijing.osm.pbf"
    write_pbf(pbf)
    meta = filter_railways(pbf, tmp_path / "beijing.railways.sqlite")
    assert meta["ways"] == "2" and meta["stations"] == "2"
    south = read_cell(tmp_path / "beijing.railways.sqlite", 210, 97)
    ids = sorted((e["type"], e["id"]) for e in south)
    assert ids == [("node", 4), ("way", 100), ("way", 101), ("way", 103)]
    line = next(e for e in south if e["id"] == 100)
    assert line["tags"]["maxspeed"] == "350" and line["coords"].shape == (3, 2), "complete geometry, like Overpass"
    area = next(e for e in south if e["id"] == 103)
    assert area["type"] == "way" and "coords" not in area and 116.30 <= area["lon"] <= 116.31, "station area: its centre"
    north = read_cell(tmp_path / "beijing.railways.sqlite", 210, 96)
    assert [e["id"] for e in north] == [100], "a line crossing two cells is in both"


def test_extract_elements_build_a_cell(tmp_path):
    pbf = tmp_path / "beijing.osm.pbf"
    write_pbf(pbf)
    filter_railways(pbf, tmp_path / "r.sqlite")
    result = build_cell(read_cell(tmp_path / "r.sqlite", 210, 97), 210, 97, tmp_path / "rail.sqlite")
    assert result.lines == 2 and result.tiles > 0
    assert [s[3] for s in result.stations] == ["北京南", "丰台"] or sorted(s[3] for s in result.stations) == ["丰台", "北京南"]


# -- planning --------------------------------------------------------------------------------


def test_plan_uses_the_smallest_regions_and_parents_when_worth_it():
    index = RegionIndex.from_json(index_json("https://x"))
    size_of = lambda region: {"china": 300e6}.get(region.id, 50e6)  # noqa: E731
    assert plan(index, [(210, 96)], size_of) == {"210-96": ["beijing"]}, "inside one province: just that one"
    assert sorted(plan(index, [(211, 97)], size_of)["211-97"]) == ["east1", "tianjin"], "on a border: both"
    # four of six provinces (200 MB) are still clearly less than the country (300 MB)
    some = plan(index, [(210, 96), (211, 97), (200, 100)], size_of)
    assert sorted({r for regions in some.values() for r in regions}) == ["beijing", "east1", "tianjin", "west2"]
    # every province needed: the country is one download of about the same size
    cells = [(210, 96), (211, 97), (200, 100), (185, 100), (218, 100)]  # all six provinces
    every = plan(index, cells, size_of)
    assert len(every) == 5 and all(regions == ["china"] for regions in every.values()), every


def test_plan_without_sizes_takes_the_parent_for_half_the_children():
    index = RegionIndex.from_json(index_json("https://x"))
    cells = [(210, 96), (211, 97), (200, 100)]  # beijing, tianjin + east1, west2: 4 of 6
    assert all(regions == ["china"] for regions in plan(index, cells, lambda region: None).values())


def test_plan_skips_regions_too_big_for_their_use():
    index = RegionIndex.from_json(index_json("https://x"))
    sizes = {"beijing": 40e6, "tianjin": 20e6, "east1": 2000e6, "china": 3000e6}
    result = plan(index, [(210, 96), (211, 97)], lambda region: sizes.get(region.id))
    assert result == {"210-96": ["beijing"]}, "211-97 needs a 2 GB region for one cell: Overpass instead"


def test_groups_of_countries_and_border_slivers_are_not_downloaded():
    """Real indexes have regions that overlap (Geofabrik's "dach", "alps", "us-west")
    and parents whose polygons stick out a little beyond their children."""
    west, south, east, north = cell_bounds(210, 97)
    mid = (west + east) / 2
    index = RegionIndex.from_json({"type": "FeatureCollection", "features": [
        feature("asia", None, square(60, -10, 150, 60), "https://x/asia.osm.pbf"),
        feature("china", "asia", square(100, 30, 125, 45), "https://x/china.osm.pbf"),
        # the provinces leave a strip 0.01° wide along the middle of the cell
        feature("hebei", "china", square(west - 1, south - 1, mid - 0.005, north + 1), "https://x/hebei.osm.pbf"),
        feature("tianjin", "china", square(mid + 0.005, south - 1, east + 1, north + 1), "https://x/tianjin.osm.pbf"),
        feature("jingjinji", "asia", square(west - 2, south - 2, east + 2, north + 2), "https://x/jingjinji.osm.pbf"),
    ]})
    assert sorted(r.id for r in index.most_specific(210, 97)) == ["hebei", "tianjin"]


def test_a_narrow_strip_of_a_neighbour_is_still_downloaded():
    west, south, east, north = cell_bounds(210, 97)
    index = RegionIndex.from_json({"type": "FeatureCollection", "features": [
        feature("asia", None, square(60, -10, 150, 60), "https://x/asia.osm.pbf"),
        feature("small", "asia", square(west - 1, south - 1, east + 1, north - 0.08), "https://x/small.osm.pbf"),
        feature("big", "asia", square(west - 30, north - 0.08, east + 30, north + 20), "https://x/big.osm.pbf"),
    ]})
    assert sorted(r.id for r in index.most_specific(210, 97)) == ["big", "small"]


def test_a_region_polygon_that_crosses_itself_is_repaired():
    """Seen in Geofabrik's real index: GEOS refuses to intersect such a polygon."""
    west, south, east, north = cell_bounds(210, 97)
    bowtie = {"type": "Polygon", "coordinates": [[
        [west - 1, south - 1], [east + 1, north + 1], [east + 1, south - 1], [west - 1, north + 1], [west - 1, south - 1],
    ]]}
    index = RegionIndex.from_json({"type": "FeatureCollection", "features": [
        feature("asia", None, square(60, -10, 150, 60), "https://x/asia.osm.pbf"),
        feature("anhui", "asia", bowtie, "https://x/anhui.osm.pbf"),
    ]})
    assert [r.id for r in index.most_specific(210, 97)] == ["anhui"]


def test_sea_around_provinces_or_states_does_not_bring_in_the_country_or_group():
    """Seen in the real index: China's, Japan's and "us-northeast"'s polygons reach
    further into the sea than their parts, so coastal cells picked them as well."""
    west, south, east, north = cell_bounds(210, 97)
    mid = (west + east) / 2
    index = RegionIndex.from_json({"type": "FeatureCollection", "features": [
        feature("asia", None, square(60, -10, 150, 60), "https://x/asia.osm.pbf"),
        # the country and the group reach 0.4° (about 40 km) further east, into the sea
        feature("china", "asia", square(100, 30, east + 0.4, 45), "https://x/china.osm.pbf"),
        feature("hebei", "china", square(100, 30, mid, 45), "https://x/hebei.osm.pbf"),
        feature("tianjin", "china", square(mid, 30, east - 0.4, 45), "https://x/tianjin.osm.pbf"),
        feature("north-china", "asia", square(99, 29, east + 0.4, 46), "https://x/north-china.osm.pbf"),
    ]})
    assert sorted(r.id for r in index.most_specific(210, 97)) == ["hebei", "tianjin"]


def test_a_country_around_a_small_neighbour_is_still_downloaded():
    """Geofabrik's Switzerland surrounds Liechtenstein: both are needed for that cell."""
    west, south, east, north = cell_bounds(210, 97)
    index = RegionIndex.from_json({"type": "FeatureCollection", "features": [
        feature("europe", None, square(60, -10, 150, 60), "https://x/europe.osm.pbf"),
        feature("switzerland", "europe", square(west - 3, south - 3, east + 3, north + 3), "https://x/ch.osm.pbf"),
        feature("liechtenstein", "europe", square(west + 0.2, south + 0.2, west + 0.5, south + 0.5),
                "https://x/li.osm.pbf"),
    ]})
    assert sorted(r.id for r in index.most_specific(210, 97)) == ["liechtenstein", "switzerland"]


def test_too_big_regions_are_left_out_until_nothing_changes():
    """A region left with fewer cells after another one was left out is checked again."""
    index = RegionIndex.from_json(index_json("https://x"))
    sizes = {"beijing": 30e6, "tianjin": 150e6, "east1": 150e6, "china": 9000e6}
    # tianjin serves 210-97 and 211-97 (75 MB each); 211-97 also needs east1 (150 MB for one cell)
    result = plan(index, [(210, 97), (211, 97)], lambda region: sizes.get(region.id))
    assert result == {}, "without 211-97, tianjin is 150 MB for one cell"


def test_continents_are_never_downloaded():
    index = RegionIndex.from_json(index_json("https://x"))
    # 20, 20 is outside every country: nothing to download
    assert index.most_specific(20, 20) == []
    assert all(r.id != "asia" for x in range(205, 216) for r in index.most_specific(x, 97))


# -- downloads -----------------------------------------------------------------------------------


@pytest.fixture
def geofabrik(tmp_path):
    server = FakeServer().start()
    pbf = tmp_path / "served.osm.pbf"
    write_pbf(pbf)
    data = pbf.read_bytes()
    server.routes["/index-v1.json"] = lambda r: (200, {"Content-Type": "application/json"},
                                                json.dumps(index_json(server.url)).encode())

    def pbf_route(rec):  # the fake server sets Content-Length (and sends no body for HEAD)
        if rec.headers.get("Range"):
            start = int(rec.headers["Range"].split("=")[1].split("-")[0])
            return 206, {}, data[start:]
        return 200, {}, data

    for name in ("beijing", "tianjin", "hebei", "china"):
        server.routes[f"/{name}.osm.pbf"] = pbf_route
        server.routes[f"/{name}.osm.pbf.md5"] = lambda r: (200, {}, f"{hashlib.md5(data).hexdigest()}  x.osm.pbf\n".encode())
    server.data = data
    yield server
    server.stop()


def make_store(home, geofabrik) -> ExtractStore:
    import requests

    return ExtractStore(home, requests.Session(), index_url=geofabrik.url + "/index-v1.json")


def no_activity(*args, **kwargs):
    pass


def downloads(server, name: str) -> int:
    return sum(1 for r in server.requests if r.method == "GET" and r.path == f"/{name}.osm.pbf")


def test_region_is_downloaded_once_filtered_and_reused(home, geofabrik):
    store = make_store(home, geofabrik)
    elements = store.cell_elements(210, 96, ["beijing"], threading.Event(), no_activity, lambda n: None)
    assert [e["id"] for e in elements] == [100]
    assert store.db_path("beijing").exists()
    assert not list(store.folder.glob("*.pbf*")), "the downloaded extract is deleted after filtering"
    store.cell_elements(210, 97, ["beijing"], threading.Event(), no_activity, lambda n: None)
    assert downloads(geofabrik, "beijing") == 1, "filtered once, then reused"


def test_cells_waiting_for_the_same_region_share_one_download(home, geofabrik):
    store = make_store(home, geofabrik)
    results = {}

    def get(cell):
        results[cell] = store.cell_elements(*cell, ["beijing"], threading.Event(), no_activity, lambda n: None)

    threads = [threading.Thread(target=get, args=((210, y),)) for y in (96, 97)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(results) == 2
    assert downloads(geofabrik, "beijing") == 1


def test_a_broken_download_is_not_used(home, geofabrik):
    geofabrik.routes["/beijing.osm.pbf.md5"] = lambda r: (200, {}, b"0" * 32 + b"  x\n")
    store = make_store(home, geofabrik)
    with pytest.raises(ExtractUnavailable):
        store.cell_elements(210, 96, ["beijing"], threading.Event(), no_activity, lambda n: None)
    assert not store.db_path("beijing").exists()
    assert not store.usable("beijing"), "cells use Overpass for a while"


def test_an_interrupted_download_is_resumed(home, geofabrik):
    store = make_store(home, geofabrik)
    home.ensure_dir(store.folder)
    (store.folder / "beijing.osm.pbf.part").write_bytes(geofabrik.data[:100])
    store.cell_elements(210, 96, ["beijing"], threading.Event(), no_activity, lambda n: None)
    request = next(r for r in geofabrik.requests if r.path == "/beijing.osm.pbf" and r.method == "GET")
    assert request.headers.get("Range") == "bytes=100-"


def gets(server, name: str) -> list:
    return [r for r in server.requests if r.method == "GET" and r.path == f"/{name}.osm.pbf"]


def test_a_broken_connection_is_continued_from_where_it_broke(home, geofabrik, monkeypatch):
    monkeypatch.setattr(extracts, "RETRY_DELAYS", (0.01, 0.01))
    monkeypatch.setattr(extracts, "CHUNK", 64)
    data = geofabrik.data
    tag = {"ETag": '"v1"'}

    def route(rec):
        if rec.headers.get("Range"):
            start = int(rec.headers["Range"].split("=")[1].split("-")[0])
            return 206, tag, data[start:]
        return 200, {**tag, "X-Cut-After": str(len(data) // 2)}, data  # the connection breaks

    geofabrik.routes["/beijing.osm.pbf"] = route
    store = make_store(home, geofabrik)
    elements = store.cell_elements(210, 96, ["beijing"], threading.Event(), no_activity, lambda n: None)
    assert [e["id"] for e in elements] == [100]
    first, second = gets(geofabrik, "beijing")
    assert "Range" not in first.headers
    start = int(second.headers["Range"].split("=")[1].split("-")[0])
    assert 0 < start <= len(data) // 2 and second.headers.get("If-Range") == '"v1"', "the same version, continued"


def test_a_partial_file_of_an_older_version_is_downloaded_again(home, geofabrik):
    store = make_store(home, geofabrik)
    home.ensure_dir(store.folder)
    (store.folder / "beijing.osm.pbf.part").write_bytes(b"x" * 100)  # yesterday's file, say
    store.cell_elements(210, 96, ["beijing"], threading.Event(), no_activity, lambda n: None)
    first, second = gets(geofabrik, "beijing")
    assert first.headers.get("Range") == "bytes=100-" and "Range" not in second.headers
    assert store.db_path("beijing").exists() and store.usable("beijing")


def test_leftovers_are_removed_at_start_but_recent_downloads_continue(home, geofabrik):
    import os

    store = make_store(home, geofabrik)
    home.ensure_dir(store.folder)
    recent = store.folder / "beijing.osm.pbf.part"
    old = store.folder / "hebei.osm.pbf.part"
    unfiltered = store.folder / "tianjin.railways.part"
    for path in (recent, old, unfiltered):
        path.write_bytes(b"x")
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    store.remove_stale()
    assert recent.exists() and not old.exists() and not unfiltered.exists()


def test_plan_from_the_index(home, geofabrik):
    store = make_store(home, geofabrik)
    assert store.plan([(210, 96)]) == {"210-96": ["beijing"]}
    assert geofabrik.count("/index-v1.json") == 1


def test_old_regions_are_removed(home, geofabrik):
    import os

    store = make_store(home, geofabrik)
    store.cell_elements(210, 96, ["beijing"], threading.Event(), no_activity, lambda n: None)
    old = time.time() - 8 * 86400
    os.utime(store.db_path("beijing"), (old, old))
    assert not store.fresh("beijing") and not store.any_fresh()
    store.remove_stale()
    assert not store.db_path("beijing").exists()


def test_cell_bounds_match_tiles():
    assert cell_bounds(210, 97) == tile_bounds(8, 210, 97)
    assert cell_id(210, 97) == "210-97"
