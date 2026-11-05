"""Lines under construction in uncached areas (setting 「永远显示在建线路」, SPEC §7.9)."""

from __future__ import annotations

import json
import re
import threading
import time
from urllib.parse import parse_qs

import mapbox_vector_tile
import pytest
import requests

from fakeserver import FakeServer
from offline_railway_map import construction
from offline_railway_map.construction import ConstructionLines, data_zoom
from offline_railway_map.coverage import CellCoverage
from offline_railway_map.grid import lonlat_to_tile
from offline_railway_map.overpass import OverpassClient
from offline_railway_map.store import StateStore
from railfixtures import way

BEIJING = (116.4, 39.9)


def ways() -> list[dict]:
    return [
        # a high-speed line under construction near Beijing: drawn at every zoom
        way(1, [(116.1, 39.6), (116.7, 40.2)], railway="construction", construction="rail", usage="main",
            maxspeed="350", name="在建高铁"),
        # a subway under construction: from z10, where the ORM tiles draw it
        way(2, [(116.38, 39.89), (116.42, 39.91)], railway="construction", construction="subway"),
        # built lines are not part of the overlay (ORM's tiles draw them)
        way(3, [(116.1, 39.7), (116.5, 40.0)], railway="rail", usage="main", maxspeed="200"),
        # a siding under construction: from z13, where the ORM tiles draw it
        way(4, [(116.40, 39.90), (116.41, 39.905)], railway="construction", **{"construction:railway": "rail"},
            service="siding"),
    ]


def asked_for(query: str, element: dict) -> bool:
    """What our query selects: railway lines under construction without service."""
    tags = element["tags"]
    rail = tags.get("construction") == "rail" or tags.get("construction:railway") == "rail"
    return tags.get("railway") == "construction" and rail and "service" not in tags


def bbox_of(query: str) -> tuple[float, float, float, float]:
    s, w, n, e = (float(v) for v in re.search(r"\]\(([^)]*)\);", query).group(1).split(","))
    return s, w, n, e


class Overpass:
    """Answers our construction query from ``ways()``; refuses areas wider than ``max_width``."""

    def __init__(self, max_width: float = 360.0, delay: float = 0.0) -> None:
        self.max_width = max_width
        self.delay = delay
        self.server = FakeServer()
        self.server.routes["/api/interpreter"] = self.answer
        self.server.default = lambda rec: (200, {"Content-Type": "text/plain"}, b"Rate limit: 2\n")
        self.server.start()
        self.queries: list[tuple[float, float, float, float]] = []

    def answer(self, rec):
        query = parse_qs(rec.body.decode())["data"][0]
        assert 'way["railway"="construction"]' in query
        s, w, n, e = bbox_of(query)
        self.queries.append((s, w, n, e))
        time.sleep(self.delay)
        if e - w > self.max_width:
            body = {"elements": [], "remark": "runtime error: Query run out of memory using about 2048 MB of RAM."}
        else:
            found = [x for x in ways() if any(w <= p["lon"] <= e and s <= p["lat"] <= n for p in x["geometry"])]
            body = {"elements": [x for x in found if asked_for(query, x)]}
        return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()

    @property
    def url(self) -> str:
        return self.server.url + "/api/interpreter"


@pytest.fixture
def overpass():
    fake = Overpass()
    yield fake
    fake.server.stop()


CREATED: list[ConstructionLines] = []


@pytest.fixture(autouse=True)
def stop_fetching():
    yield
    while CREATED:
        CREATED.pop().stop(timeout=2.0)


class Switch:
    def __init__(self, on: bool = True) -> None:
        self.on = on

    def __call__(self) -> bool:
        return self.on


def make(home, fake: Overpass, switch: Switch | None = None, coverage: CellCoverage | None = None):
    client = OverpassClient(
        requests.Session(), lambda: [fake.url], StateStore(home), min_interval=0,
        server_slots=lambda endpoint: 2, busy_backoff=(0.05,), busy_response_seconds=0.0,
    )
    changes: list[int] = []
    lines = ConstructionLines(home, client, coverage or CellCoverage(), switch or Switch(), on_change=changes.append)
    lines.changes = changes
    CREATED.append(lines)
    return lines


def tile_at(z: int, lon: float = BEIJING[0], lat: float = BEIJING[1]) -> tuple[int, int, int]:
    x, y = lonlat_to_tile(lon, lat, z)
    return z, int(x), int(y)


def features(data: bytes) -> list[dict]:
    if not data:
        return []
    return mapbox_vector_tile.decode(data).get("rail", {}).get("features", [])


def wait_for(lines: ConstructionLines, z: int, timeout: float = 20.0, **where) -> bytes:
    """Ask for a tile until it has data (the area is fetched in the background)."""
    end = time.time() + timeout
    while time.time() < end:
        data = lines.tile(*tile_at(z, **where))
        if data:
            return data
        time.sleep(0.05)
    raise AssertionError("no construction lines arrived")


def test_switched_off_nothing_is_asked_for(home, overpass):
    lines = make(home, overpass, Switch(False))
    assert lines.tile(*tile_at(6)) == b""
    time.sleep(0.2)
    assert overpass.queries == [] and not home.data_exists()


def test_lines_under_construction_at_every_zoom_drawn_like_built_lines(home, overpass):
    lines = make(home, overpass)
    assert lines.tile(*tile_at(5)) == b"", "nothing yet: the area is fetched in the background"
    wait_for(lines, 5)
    for z in range(2, 15):
        found = features(wait_for(lines, z))
        ids = {f["id"] for f in found}
        assert 1 in ids, f"the high-speed line is shown at z{z}"
        line = next(f for f in found if f["id"] == 1)
        assert line["properties"]["st"] == "c" and line["properties"]["sp"] == 350, "coloured by its speed"
        assert ids == {1}, "built lines, subways and sidings: the ORM tiles draw them where they appear"
    assert lines.changes, "the page is told to reload the overlay"
    assert (home.cache_dir / "construction.sqlite").exists()


def test_zooming_in_asks_for_nothing(home, overpass):
    """From z9 the ORM tiles draw lines under construction: nothing to wait for."""
    lines = make(home, overpass)
    assert lines.tile(*tile_at(12)) == b""
    time.sleep(0.3)
    assert overpass.queries == []
    wait_for(lines, 8)
    s, w, n, e = overpass.queries[0]
    assert data_zoom(8) == 5 and e - w == pytest.approx(360 / 2**5), "a z8 view asks for its z5 area"
    asked = len(overpass.queries)
    assert {f["id"] for f in features(lines.tile(*tile_at(12)))} == {1}, "drawn from what the overview fetched"
    time.sleep(0.3)
    assert len(overpass.queries) == asked


def test_inside_cached_cells_the_cell_draws_its_own_lines(home, overpass):
    coverage = CellCoverage()
    lines = make(home, overpass, coverage=coverage)
    wait_for(lines, 8)
    assert features(lines.tile(*tile_at(10)))
    z, x, y = tile_at(10)
    coverage.replace({(x >> 2, y >> 2)})
    assert lines.tile(z, x, y) == b""
    assert lines.tile(*tile_at(6)), "lower zooms keep the overlay (drawn below the cached lines)"


def test_fetched_areas_are_kept_and_used_after_a_restart(home, overpass):
    wait_for(make(home, overpass), 7)
    asked = len(overpass.queries)
    again = make(home, overpass)
    assert features(again.tile(*tile_at(7))), "shown straight from disk"
    time.sleep(0.3)
    assert len(overpass.queries) == asked, "nothing fetched again within 7 days"


def test_old_areas_are_fetched_again(home, overpass, monkeypatch):
    wait_for(make(home, overpass), 7)
    asked = len(overpass.queries)
    monkeypatch.setattr(construction, "FRESH_FOR", 0.0)
    again = make(home, overpass)
    assert features(again.tile(*tile_at(7))), "the old data is shown meanwhile"
    end = time.time() + 10
    while len(overpass.queries) == asked and time.time() < end:
        time.sleep(0.05)
    assert len(overpass.queries) > asked


def test_an_area_too_big_for_the_servers_is_asked_for_in_quarters(home):
    fake = Overpass(max_width=30.0)  # z3 areas (45°) are refused, z4 areas (22.5°) answered
    try:
        lines = make(home, fake)
        found = features(wait_for(lines, 4))
        assert 1 in {f["id"] for f in found}
        widths = [round(e - w, 3) for s, w, n, e in fake.queries]
        assert 45.0 in widths and 22.5 in widths
        refused = widths.count(45.0)
        lines.tile(*tile_at(4))
        lines.tile(*tile_at(3))
        time.sleep(0.3)
        assert [round(e - w, 3) for s, w, n, e in fake.queries].count(45.0) == refused, "not asked again"
    finally:
        fake.server.stop()


def test_switching_off_while_fetching_saves_nothing(home):
    fake = Overpass(delay=0.5)
    switch = Switch(True)
    try:
        lines = make(home, fake, switch)
        lines.tile(*tile_at(6))
        end = time.time() + 5
        while not fake.queries and time.time() < end:
            time.sleep(0.01)
        switch.on = False
        lines.settings_changed()
        time.sleep(1.0)  # the answer arrives after switching off
        assert not (home.cache_dir / "construction.sqlite").exists()
        switch.on = True
        assert lines.tile(*tile_at(6)) == b"", "not drawn from an abandoned answer"
    finally:
        fake.server.stop()


def test_stop_abandons_a_running_query(home):
    fake = Overpass(delay=0.5)
    try:
        lines = make(home, fake)
        lines.tile(*tile_at(6))
        end = time.time() + 5
        while not fake.queries and time.time() < end:
            time.sleep(0.01)
        lines.stop(timeout=0)
        lines.close()
        time.sleep(1.0)
        assert not (home.cache_dir / "construction.sqlite").exists(), "nothing saved after stop()"
        assert not any(t.name == "construction" and t.is_alive() for t in threading.enumerate())
    finally:
        fake.server.stop()
