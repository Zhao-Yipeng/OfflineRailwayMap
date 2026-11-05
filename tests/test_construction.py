"""Lines under construction across China (setting 「永远显示在建线路」, SPEC §7.9).

A stand-in Geofabrik serves the same small extract for each of the six
"provinces" of test_extracts.index_json ("china" > west1, west2, beijing,
tianjin, east1, east2; "mongolia" is outside China).
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


def write_pbf(path: Path) -> None:
    with osmium.SimpleWriter(str(path)) as w:
        for i, (lon, lat) in enumerate([(116.1, 39.6), (116.4, 39.9), (116.7, 40.2)], 1):
            w.add_node(Node(id=i, location=(lon, lat)))
        w.add_node(Node(id=11, location=(116.38, 39.89)))
        w.add_node(Node(id=12, location=(116.42, 39.91)))
        w.add_node(Node(id=21, location=(116.1, 39.7)))
        w.add_node(Node(id=22, location=(116.5, 40.0)))
        w.add_node(Node(id=31, location=(116.40, 39.90)))
        w.add_node(Node(id=32, location=(116.41, 39.905)))
        # a high-speed line under construction: drawn at every zoom
        w.add_way(Way(id=100, nodes=[1, 2, 3], tags={"railway": "construction", "construction": "rail",
                                                     "usage": "main", "maxspeed": "350", "name": "在建高铁"}))
        # from z10 / z13 the ORM tiles draw these: not part of the overlay
        w.add_way(Way(id=101, nodes=[11, 12], tags={"railway": "construction", "construction": "subway"}))
        w.add_way(Way(id=103, nodes=[31, 32], tags={"railway": "construction", "construction:railway": "rail",
                                                    "service": "siding"}))
        # built lines come from the ORM tiles
        w.add_way(Way(id=102, nodes=[21, 22], tags={"railway": "rail", "usage": "main", "maxspeed": "200"}))


class Geofabrik:
    """Index, extracts and checksums; ``slow`` regions take ``delay`` seconds to answer."""

    def __init__(self, tmp_path: Path) -> None:
        pbf = tmp_path / "served.osm.pbf"
        write_pbf(pbf)
        self.data = pbf.read_bytes()
        self.slow: dict[str, float] = {}
        self.server = FakeServer().start()
        self.server.routes["/index-v1.json"] = lambda r: (
            200, {"Content-Type": "application/json"}, json.dumps(index_json(self.server.url)).encode())
        md5 = hashlib.md5(self.data).hexdigest()
        for name in (*PROVINCES, "china", "mongolia", "asia"):
            self.server.routes[f"/{name}.osm.pbf"] = self._route(name)
            self.server.routes[f"/{name}.osm.pbf.md5"] = lambda r: (200, {}, f"{md5}  x.osm.pbf\n".encode())

    def _route(self, name: str):
        def route(rec):
            if rec.method == "GET":
                time.sleep(self.slow.get(name, 0.0))
            return 200, {}, self.data
        return route

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


def make(home, geofabrik, switch=None, coverage=None, planned=frozenset(), center=(116.4, 39.9)) -> ConstructionLines:
    store = ExtractStore(home, requests.Session(), index_url=geofabrik.server.url + "/index-v1.json")
    lines = ConstructionLines(
        home, store, coverage or CellCoverage(), switch or Switch(),
        on_change=lambda version: None, planned_regions=lambda: set(planned), view_center=lambda: center,
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
        assert ids(data) == {100}, f"z{z}: the line under construction, nothing else"
        feature = mapbox_vector_tile.decode(data)["rail"]["features"][0]
        assert feature["properties"]["st"] == "c" and feature["properties"]["sp"] == 350, "coloured by its speed"
    assert lines.tile(*tile_at(6, lon=10.0, lat=50.0)) == b"", "outside China: nothing"


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
    assert ids(lines.tile(*tile_at(9))) == {100}, "the lines are kept"
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


def test_inside_cached_cells_the_cell_draws_its_own_lines(home, geofabrik):
    coverage = CellCoverage()
    lines = make(home, geofabrik, coverage=coverage)
    lines.settings_changed()
    wait_for_question(lines)
    z, x, y = tile_at(10)
    coverage.replace({(x >> 2, y >> 2)})
    assert lines.tile(z, x, y) == b""
    assert ids(lines.tile(*tile_at(6))) == {100}, "lower zooms keep the overlay (drawn below the cached lines)"


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
    assert lines._built_at("tianjin") > before
