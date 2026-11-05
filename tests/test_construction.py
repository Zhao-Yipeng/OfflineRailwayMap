"""Lines under construction across China (setting 「永远显示在建线路」, SPEC §7.9).

A stand-in Geofabrik serves the same small extract for each of the six
"provinces" of test_extracts.index_json ("china" > west1, west2, beijing,
tianjin, east1, east2; "mongolia" is outside China) and for all of China,
which cell downloads of many cells may choose.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import mapbox_vector_tile
import osmium
import pytest
import requests
from osmium.osm.mutable import Node, Way

from fakeserver import FakeServer
from offline_railway_map import construction
from offline_railway_map.construction import ConstructionLines
from offline_railway_map.coverage import CellCoverage
from offline_railway_map.extracts import ExtractStore
from offline_railway_map.grid import lonlat_to_tile
from test_extracts import index_json

PROVINCES = ("west1", "west2", "beijing", "tianjin", "east1", "east2")
ON_LINE = (116.4, 39.9)  # a point on the high-speed line under construction


def write_pbf(path: Path, opened: bool = False, deleted: bool = True) -> None:
    """``opened``: the high-speed line (100) is in service now; ``deleted``: without the
    line 106 (in "beijing", far from its borders)."""
    with osmium.SimpleWriter(str(path)) as w:
        for i, (lon, lat) in enumerate([(116.1, 39.6), (116.4, 39.9), (116.7, 40.2)], 1):
            w.add_node(Node(id=i, location=(lon, lat)))
        w.add_node(Node(id=11, location=(116.38, 39.89)))
        w.add_node(Node(id=12, location=(116.42, 39.91)))
        w.add_node(Node(id=21, location=(116.1, 39.7)))
        w.add_node(Node(id=22, location=(116.5, 40.0)))
        w.add_node(Node(id=31, location=(116.40, 39.90)))
        w.add_node(Node(id=32, location=(116.41, 39.905)))
        w.add_node(Node(id=41, location=(133.0, 45.0)))
        w.add_node(Node(id=42, location=(137.0, 45.0)))
        w.add_node(Node(id=51, location=(116.2, 40.3)))
        w.add_node(Node(id=52, location=(116.6, 40.6)))
        # a high-speed line under construction: drawn at every zoom
        if opened:
            w.add_way(Way(id=100, nodes=[1, 2, 3], tags={"railway": "rail", "usage": "main", "maxspeed": "350"}))
        else:
            w.add_way(Way(id=100, nodes=[1, 2, 3], tags={"railway": "construction", "construction": "rail",
                                                         "usage": "main", "maxspeed": "350", "name": "在建高铁"}))
        if not deleted:
            w.add_way(Way(id=106, nodes=[51, 52], tags={"railway": "construction", "construction": "rail",
                                                        "usage": "main", "maxspeed": "250"}))
        # from z10 / z13 the ORM tiles draw these: not part of the overlay
        w.add_way(Way(id=101, nodes=[11, 12], tags={"railway": "construction", "construction": "subway"}))
        w.add_way(Way(id=103, nodes=[31, 32], tags={"railway": "construction", "construction:railway": "rail",
                                                    "service": "siding"}))
        # built lines come from the ORM tiles
        w.add_way(Way(id=102, nodes=[21, 22], tags={"railway": "rail", "usage": "main", "maxspeed": "200"}))
        # crosses the border of China (lon 135 in index_json): only the part inside is drawn
        w.add_way(Way(id=104, nodes=[41, 42], tags={"railway": "construction", "construction": "rail",
                                                    "usage": "main", "maxspeed": "250"}))


class Geofabrik:
    """Index, extracts and checksums; ``slow`` regions take ``delay`` seconds to answer."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        pbf = tmp_path / "served.osm.pbf"
        write_pbf(pbf)
        self.data = pbf.read_bytes()
        self.data_for: dict[str, bytes] = {}
        self.slow: dict[str, float] = {}
        self.server = FakeServer().start()
        self.server.routes["/index-v1.json"] = lambda r: (
            200, {"Content-Type": "application/json"}, json.dumps(index_json(self.server.url)).encode())
        for name in (*PROVINCES, "china", "mongolia", "asia"):
            self.server.routes[f"/{name}.osm.pbf"] = self._route(name)
            self.server.routes[f"/{name}.osm.pbf.md5"] = self._md5_route(name)

    def serve(self, name: str, **changes) -> None:
        """Another extract for this region (see write_pbf)."""
        pbf = self.tmp_path / f"{name}-changed.osm.pbf"
        write_pbf(pbf, **changes)
        self.data_for[name] = pbf.read_bytes()

    def _data(self, name: str) -> bytes:
        return self.data_for.get(name, self.data)

    def _route(self, name: str):
        def route(rec):
            if rec.method == "GET":
                time.sleep(self.slow.get(name, 0.0))
            return 200, {}, self._data(name)
        return route

    def _md5_route(self, name: str):
        return lambda rec: (200, {}, f"{hashlib.md5(self._data(name)).hexdigest()}  x.osm.pbf\n".encode())

    def downloads(self, name: str) -> int:
        return sum(1 for r in self.server.requests if r.method == "GET" and r.path == f"/{name}.osm.pbf")

    def downloaded(self) -> set[str]:
        return {r.path[1:].split(".")[0] for r in self.server.requests if r.method == "GET" and r.path.endswith(".osm.pbf")}


@pytest.fixture
def geofabrik(tmp_path):
    fake = Geofabrik(tmp_path)
    yield fake
    fake.server.stop()


class Switch:
    def __init__(self, on: bool = True) -> None:
        self.on = on

    def __call__(self) -> bool:
        return self.on


CREATED: list[ConstructionLines] = []


@pytest.fixture(autouse=True)
def stop_everything():
    yield
    while CREATED:
        lines = CREATED.pop()
        lines.stop(timeout=5.0)
        lines.extracts.stop(timeout=5.0)
        lines.close()


def make(home, geofabrik, switch=None, coverage=None, planned=frozenset(), center=(116.4, 39.9),
         cells_busy=lambda: False) -> ConstructionLines:
    store = ExtractStore(home, requests.Session(), index_url=geofabrik.server.url + "/index-v1.json")
    lines = ConstructionLines(
        home, store, coverage or CellCoverage(), switch or Switch(),
        on_change=lambda version: None, planned_regions=lambda: set(planned), view_center=lambda: center,
        cells_busy=cells_busy,
    )
    lines.questions = []
    lines.on_question = lines.questions.append
    CREATED.append(lines)
    return lines


def wait_for_question(lines: ConstructionLines, timeout: float = 30.0) -> dict | None:
    """Wait for the pass started last to finish; the question it asked, if any."""
    asked = len(lines.questions)
    thread = lines._thread
    if thread is not None:
        thread.join(timeout)
        assert not thread.is_alive(), "the pass did not finish"
    return lines.questions[-1] if len(lines.questions) > asked or (asked and thread is None) else None


def tile_at(z: int, lon: float = ON_LINE[0], lat: float = ON_LINE[1]) -> tuple[int, int, int]:
    x, y = lonlat_to_tile(lon, lat, z)
    return z, int(x), int(y)


def ids(data: bytes) -> set[int]:
    if not data:
        return set()
    return {f["id"] for f in mapbox_vector_tile.decode(data).get("rail", {}).get("features", [])}


def test_switched_off_nothing_is_downloaded(home, geofabrik):
    lines = make(home, geofabrik, Switch(False))
    lines.start()
    assert lines.tile(*tile_at(6)) == b""
    time.sleep(0.3)
    assert geofabrik.server.requests == [] and not home.data_exists()


def test_switching_on_downloads_china_once_and_draws_its_lines_at_every_zoom(home, geofabrik):
    lines = make(home, geofabrik)
    lines.settings_changed()
    question = wait_for_question(lines)
    assert geofabrik.downloaded() == set(PROVINCES), "every province once; nothing outside China"
    assert all(geofabrik.downloads(p) == 1 for p in PROVINCES)
    assert sorted(question["regions"]) == sorted(PROVINCES) and question["bytes"] > 0
    for z in range(2, 15):
        data = lines.tile(*tile_at(z))
        found = ids(data)
        assert 100 in found and not found & {101, 102, 103}, f"z{z}: the line under construction, nothing else"
        feature = next(f for f in mapbox_vector_tile.decode(data)["rail"]["features"] if f["id"] == 100)
        assert feature["properties"]["st"] == "c" and feature["properties"]["sp"] == 350, "coloured by its speed"
    assert lines.tile(*tile_at(6, lon=10.0, lat=50.0)) == b"", "outside China: nothing"


def test_only_the_part_inside_china_is_drawn(home, geofabrik):
    lines = make(home, geofabrik)
    lines.settings_changed()
    wait_for_question(lines)
    assert 104 in ids(lines.tile(*tile_at(9, lon=134.0, lat=45.0))), "inside China"
    assert ids(lines.tile(*tile_at(9, lon=136.0, lat=45.0))) == set(), "across the border: cut off"


def test_extracts_already_on_disk_are_shared_and_not_asked_about(home, geofabrik):
    lines = make(home, geofabrik)
    lines.extracts.cell_elements(210, 96, ["beijing"], threading.Event(), lambda *a: None, lambda n: None)
    lines.settings_changed()
    question = wait_for_question(lines)
    assert geofabrik.downloads("beijing") == 1, "downloaded for cells, used by the overlay too"
    assert "beijing" not in question["regions"] and len(question["regions"]) == len(PROVINCES) - 1


def test_deleting_removes_only_what_was_downloaded_for_this(home, geofabrik):
    lines = make(home, geofabrik)
    lines.extracts.cell_elements(210, 96, ["beijing"], threading.Event(), lambda *a: None, lambda n: None)
    lines.settings_changed()
    wait_for_question(lines)
    result = lines.decide(delete=True)
    assert result["deleted"] == len(PROVINCES) - 1 and result["bytes"] > 0
    assert lines.extracts.db_path("beijing").exists(), "the cells' extract stays"
    assert not any(lines.extracts.db_path(p).exists() for p in PROVINCES if p != "beijing")
    assert 100 in ids(lines.tile(*tile_at(9))), "the lines are kept"
    assert lines.question() is None


def test_keeping_leaves_them_like_other_extracts(home, geofabrik):
    lines = make(home, geofabrik)
    lines.settings_changed()
    wait_for_question(lines)
    assert lines.decide(delete=False)["deleted"] == 0
    assert all(lines.extracts.db_path(p).exists() and lines.extracts.fresh(p) for p in PROVINCES)
    assert lines.question() is None


def test_a_region_cells_need_while_it_downloads_is_shared_and_kept(home, geofabrik):
    geofabrik.slow["tianjin"] = 1.5
    lines = make(home, geofabrik, center=(116.0, 39.0))  # tianjin first
    lines.settings_changed()
    end = time.time() + 10
    while geofabrik.downloads("tianjin") == 0 and time.time() < end:
        time.sleep(0.02)
    elements = lines.extracts.cell_elements(210, 97, ["tianjin"], threading.Event(), lambda *a: None, lambda n: None)
    assert {e["id"] for e in elements} >= {100}
    question = wait_for_question(lines)
    assert geofabrik.downloads("tianjin") == 1, "one download for both"
    assert "tianjin" not in question["regions"], "cells use it: not only for the overlay"
    lines.decide(delete=True)
    assert lines.extracts.db_path("tianjin").exists()


def test_regions_waiting_cells_may_read_are_kept(home, geofabrik):
    lines = make(home, geofabrik, planned={"east1"})
    lines.settings_changed()
    wait_for_question(lines)
    result = lines.decide(delete=True)
    assert result["kept"] == 1 and lines.extracts.db_path("east1").exists()
    assert not lines.extracts.db_path("west1").exists()


def test_cells_using_an_extract_kept_for_the_overlay_take_it_over(home, geofabrik):
    lines = make(home, geofabrik)
    lines.settings_changed()
    question = wait_for_question(lines)
    assert "beijing" in question["regions"]
    lines.extracts.cell_elements(210, 96, ["beijing"], threading.Event(), lambda *a: None, lambda n: None)
    assert geofabrik.downloads("beijing") == 1, "already there: no second download"
    assert "beijing" not in lines.question()["regions"]
    lines.decide(delete=True)
    assert lines.extracts.db_path("beijing").exists()


def test_an_unanswered_question_comes_back_after_a_restart(home, geofabrik):
    lines = make(home, geofabrik)
    lines.settings_changed()
    first = wait_for_question(lines)
    lines.stop()
    lines.close()
    again = make(home, geofabrik)
    assert again.question()["regions"] == first["regions"]
    again.start()  # nothing missing: no new pass, no new downloads
    time.sleep(0.3)
    assert all(geofabrik.downloads(p) == 1 for p in PROVINCES)


def test_switching_off_stops_and_asks_about_what_was_downloaded(home, geofabrik):
    for name in PROVINCES:
        geofabrik.slow[name] = 0.4
    switch = Switch(True)
    lines = make(home, geofabrik, switch)
    lines.settings_changed()
    end = time.time() + 10
    while len(geofabrik.downloaded()) < 2 and time.time() < end:
        time.sleep(0.02)
    switch.on = False
    lines.settings_changed()
    question = wait_for_question(lines)
    assert question is not None and 1 <= len(question["regions"]) <= 2
    time.sleep(1.0)
    assert len(geofabrik.downloaded()) <= 2, "no further provinces after switching off"


def test_cell_downloads_go_first(home, geofabrik):
    busy = Switch(True)
    lines = make(home, geofabrik, cells_busy=busy)
    lines.settings_changed()
    time.sleep(1.0)
    assert geofabrik.downloaded() == set(), "nothing while cells are being downloaded"
    busy.on = False
    wait_for_question(lines)
    assert geofabrik.downloaded() == set(PROVINCES)


def test_lines_of_an_older_version_are_fetched_again(home, geofabrik):
    """The Overpass version of this overlay left another table layout behind."""
    import sqlite3

    home.ensure_dir(home.cache_dir)
    old = sqlite3.connect(home.cache_dir / "construction.sqlite")
    old.executescript(
        "CREATE TABLE areas (z INTEGER, x INTEGER, y INTEGER, fetched_at REAL NOT NULL, PRIMARY KEY (z, x, y));"
        "CREATE TABLE lines (z INTEGER, x INTEGER, y INTEGER, id INTEGER, tags TEXT, coords BLOB);"
        "INSERT INTO areas VALUES (3, 6, 3, 1.0);"
    )
    old.commit()
    old.close()
    lines = make(home, geofabrik)
    assert lines.tile(*tile_at(6)) == b""
    lines.settings_changed()
    wait_for_question(lines)
    assert 100 in ids(lines.tile(*tile_at(6)))


def test_inside_cached_cells_the_cell_draws_its_own_lines(home, geofabrik):
    coverage = CellCoverage()
    lines = make(home, geofabrik, coverage=coverage)
    lines.settings_changed()
    wait_for_question(lines)
    z, x, y = tile_at(10)
    coverage.replace({(x >> 2, y >> 2)})
    assert lines.tile(z, x, y) == b""
    assert 100 in ids(lines.tile(*tile_at(6))), "lower zooms keep the overlay (drawn below the cached lines)"


def test_switching_on_again_refreshes_old_lines_from_extracts_on_disk(home, geofabrik, monkeypatch):
    lines = make(home, geofabrik)
    lines.settings_changed()
    wait_for_question(lines)
    lines.decide(delete=False)
    before = lines._built_at("beijing")
    monkeypatch.setattr(construction, "FRESH_FOR", 0.0)
    lines.settings_changed()
    wait_for_question(lines)
    assert lines._built_at("beijing") > before
    assert all(geofabrik.downloads(p) == 1 for p in PROVINCES), "the extracts on disk were used"
    assert len(lines.questions) == 1, "nothing new to ask about"


def test_a_province_filtered_for_cells_updates_its_lines(home, geofabrik):
    lines = make(home, geofabrik)
    lines.settings_changed()
    wait_for_question(lines)
    lines.decide(delete=True)
    before = lines._built_at("tianjin")
    lines.extracts.cell_elements(210, 97, ["tianjin"], threading.Event(), lambda *a: None, lambda n: None)
    end = time.time() + 10  # updated in the background, after the cells got their data
    while lines._built_at("tianjin") <= before and time.time() < end:
        time.sleep(0.05)
    assert lines._built_at("tianjin") > before


# -- all of China's extract, chosen by cell downloads of many cells --------------------------------


def for_cells(lines: ConstructionLines, regions: list[str], cell=(210, 96)) -> list[dict]:
    """What a cell download does with the regions its plan chose."""
    return lines.extracts.cell_elements(*cell, regions, threading.Event(), lambda *a, **k: None, lambda n: None)


def wait_until(predicate, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def line_sources(lines: ConstructionLines) -> set[str]:
    return {row[0] for row in lines._db(create=False).execute("SELECT DISTINCT region FROM lines")}


def test_all_of_china_downloaded_for_cells_serves_every_province(home, geofabrik):
    """Cells (many of them: all of China's extract) are downloaded while the overlay waits."""
    busy = Switch(True)
    lines = make(home, geofabrik, cells_busy=busy)
    lines.settings_changed()
    time.sleep(0.5)
    for_cells(lines, ["china"])
    busy.on = False
    assert wait_for_question(lines) is None, "nothing was downloaded for the overlay alone"
    assert geofabrik.downloaded() == {"china"}, "no province downloaded on top"
    for z in range(2, 15):
        assert 100 in ids(lines.tile(*tile_at(z))), f"z{z}"
    assert 104 in ids(lines.tile(*tile_at(9, lon=134.0, lat=45.0))), "inside China"
    assert ids(lines.tile(*tile_at(9, lon=136.0, lat=45.0))) == set(), "across the border: cut off"


def test_cells_choosing_all_of_china_end_the_province_downloads(home, geofabrik):
    """The overlay had begun with the provinces when cells chose all of China's extract:
    it uses that one for the rest, and asks only about what it downloaded itself."""
    released = threading.Event()
    lines = make(home, geofabrik, center=(116.4, 40.5),  # in "beijing": that one first
                 cells_busy=lambda: geofabrik.downloads("beijing") > 0 and not released.is_set())
    lines.settings_changed()
    assert wait_until(lambda: lines._built_at("beijing") > 0)
    time.sleep(0.3)  # the overlay waits for the cells now
    assert geofabrik.downloaded() == {"beijing"}
    for_cells(lines, ["china"])
    released.set()
    question = wait_for_question(lines)
    assert geofabrik.downloaded() == {"beijing", "china"}, "no further province"
    assert question["regions"] == ["beijing"], "all of China belongs to the cells"
    assert line_sources(lines) == {"china"}, "the newer extract replaced Beijing's lines"
    for z in range(2, 15):
        assert 100 in ids(lines.tile(*tile_at(z))), f"z{z}"
    lines.decide(delete=True)
    assert not lines.extracts.db_path("beijing").exists() and lines.extracts.db_path("china").exists()


def test_a_download_of_all_of_china_in_progress_is_waited_for(home, geofabrik):
    """Switched on while cells download all of China: no province is downloaded, and the
    overlay does not wait for the other cells."""
    geofabrik.slow["china"] = 1.5
    lines = make(home, geofabrik, cells_busy=lambda: True)  # cells keep downloading
    cells = threading.Thread(target=for_cells, args=(lines, ["china"]))
    cells.start()
    assert wait_until(lambda: lines.extracts.busy("china"))
    lines.settings_changed()
    question = wait_for_question(lines)
    cells.join(10)
    assert question is None
    assert geofabrik.downloaded() == {"china"} and geofabrik.downloads("china") == 1
    assert 100 in ids(lines.tile(*tile_at(6)))


def test_all_of_china_filtered_for_cells_later_replaces_the_provinces_lines(home, geofabrik):
    lines = make(home, geofabrik)
    lines.settings_changed()
    wait_for_question(lines)
    lines.decide(delete=True)
    for_cells(lines, ["china"])  # later, cells of many provinces
    assert wait_until(lambda: lines._built_at("china") > 0)  # read in the background
    assert line_sources(lines) == {"china"}
    assert 100 in ids(lines.tile(*tile_at(6))) and 100 in ids(lines.tile(*tile_at(12)))
    lines.settings_changed()  # switching on again: nothing missing, nothing downloaded
    wait_for_question(lines)
    assert all(geofabrik.downloads(p) == 1 for p in PROVINCES) and geofabrik.downloads("china") == 1


def test_the_extract_read_last_decides_about_its_lines(home, geofabrik):
    """All of China's extract first, Beijing's newer one later (both for cells): a line
    opened since and one deleted since are no longer drawn as under construction."""
    geofabrik.serve("china", deleted=False)  # 106 still there
    geofabrik.serve("beijing", opened=True)  # since then: 100 in service, 106 deleted
    lines = make(home, geofabrik)
    for_cells(lines, ["china"])
    lines.settings_changed()
    assert wait_for_question(lines) is None and geofabrik.downloaded() == {"china"}
    in_beijing = tile_at(10, lon=116.4, lat=40.45)
    assert 100 in ids(lines.tile(*tile_at(6))) and 106 in ids(lines.tile(*in_beijing))
    for_cells(lines, ["beijing"])
    assert wait_until(lambda: lines._built_at("beijing") > 0)
    assert 100 not in ids(lines.tile(*tile_at(6))) and 100 not in ids(lines.tile(*tile_at(12)))
    assert 106 not in ids(lines.tile(*in_beijing)) and 106 not in ids(lines.tile(*tile_at(6)))
    assert 104 in ids(lines.tile(*tile_at(9, lon=134.0, lat=45.0))), "elsewhere all of China's lines stay"
    # and so after a restart
    lines.stop()
    lines.close()
    again = make(home, geofabrik)
    assert 100 not in ids(again.tile(*tile_at(6))) and 104 in ids(again.tile(*tile_at(9, lon=134.0, lat=45.0)))
