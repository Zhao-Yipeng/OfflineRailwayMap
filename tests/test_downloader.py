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
        assert overpass.count("/api/interpreter") == 4 + 4, "four quarters each time"
        assert overpass.count("/api/status") == 1, "the server's slots are asked for once"
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


def record_cell_events(core) -> list[dict]:
    seen: list[dict] = []
    publish = core.events.publish

    def recording(kind, data):
        if kind == "cell":
            seen.append(data)
        publish(kind, data)

    core.events.publish = recording
    return seen


def test_download_activity_is_visible_while_it_happens(home, hooks, overpass):
    def slow(rec):
        time.sleep(0.3)
        return 200, {"Content-Type": "application/json"}, overpass_json()

    overpass.routes["/api/interpreter"] = slow
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    seen = record_cell_events(core)
    try:
        core.cells.apply(["210-97"], [], None)
        assert wait_for(lambda: core.store.cell("210-97")["status"] == "cached")
        texts = {row["activity"] for row in seen if row["activity"]}
        assert any("等待 127.0.0.1 返回数据" in text for text in texts), texts
        assert any("离线底图" in text for text in texts), texts
        assert max(row["receivedBytes"] for row in seen) > 0, "bytes show up before the cell is finished"
        assert [row["progress"] for row in seen if row["stage"] == "rail"][-1] > 0, "rail progress is published"
        final = core.cells.snapshot()["cells"][0]
        assert final["activity"] is None and final["receivedBytes"] == 0, "nothing left once installed"
    finally:
        core.shutdown()


def test_busy_servers_leave_a_visible_wait_on_the_cell(home, hooks, overpass):
    overpass.routes["/api/interpreter"] = lambda r: (504, {}, b"busy")
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    core.overpass.busy_backoff = (0.01,)
    core.overpass.busy_response_seconds = 30
    try:
        core.cells.apply(["210-97"], [], None)

        def waiting() -> bool:
            row = core.cells.snapshot()["cells"][0]
            return row["status"] == "queued" and (row["activityUntil"] or 0) > time.time() + 60

        assert wait_for(waiting, timeout=30), core.cells.snapshot()["cells"][0]
        assert "忙" in core.cells.snapshot()["cells"][0]["activity"]
    finally:
        core.shutdown()


def test_several_cells_download_at_the_same_time(home, hooks, overpass):
    """Cells are worked on in parallel (one Overpass query per server slot)."""
    import threading

    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    def slow(rec):
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.3)
        with lock:
            state["running"] -= 1
        return 200, {"Content-Type": "application/json"}, overpass_json()

    overpass.routes["/api/interpreter"] = slow
    overpass.routes["/api/status"] = lambda r: (200, {}, b"Rate limit: 2\n2 slots available now.\n")
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    try:
        core.cells.apply(["210-97", "211-97", "210-98"], [], (116, 39))
        assert wait_for(
            lambda: all(core.store.cell(c)["status"] == "cached" for c in ("210-97", "211-97", "210-98")), timeout=60
        ), [core.store.cell(c)["status"] for c in ("210-97", "211-97", "210-98")]
        assert state["peak"] == 2, f"two queries at once on a server with two slots: {state}"
        assert core.cells.snapshot()["cells"][0]["activity"] is None
    finally:
        core.shutdown()


# -- railway data from regional extracts (SPEC §9.8) -----------------------------------------


@pytest.fixture
def geofabrik(tmp_path):
    import hashlib
    import json

    from test_extracts import index_json, write_pbf

    server = FakeServer().start()
    pbf = tmp_path / "served.osm.pbf"
    write_pbf(pbf)
    data = pbf.read_bytes()
    server.routes["/index-v1.json"] = lambda r: (200, {}, json.dumps(index_json(server.url)).encode())
    for name in ("china", "beijing", "tianjin", "east1", "west1", "west2", "east2"):
        server.routes[f"/{name}.osm.pbf"] = lambda r: (200, {}, data)
        server.routes[f"/{name}.osm.pbf.md5"] = lambda r: (200, {}, hashlib.md5(data).hexdigest().encode())
    yield server
    server.stop()


SIX = ["210-96", "210-97", "211-96", "211-97", "209-96", "209-97"]


def test_six_cells_or_more_use_regional_extracts(home, hooks, overpass, geofabrik):
    overpass.routes["/api/interpreter"] = lambda r: (200, {"Content-Type": "application/json"}, overpass_json())
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    core.extracts.index_url = geofabrik.url + "/index-v1.json"
    try:
        core.cells.apply(SIX, [], (116, 39))
        assert wait_for(lambda: all(core.store.cell(c)["status"] == "cached" for c in SIX), timeout=60), [
            core.store.cell(c)["status"] for c in SIX
        ]
        assert overpass.count("/api/interpreter") == 0, "no Overpass queries"
        downloads = [r.path for r in geofabrik.requests if r.method == "GET" and r.path.endswith(".osm.pbf")]
        assert len(downloads) == len(set(downloads)) >= 1, f"each region downloaded once: {downloads}"
        assert core.rail.tile(8, 210, 97), "the cell has railway tiles"
        found = core.store.search_stations("北京南")
        assert [s["name"] for s in found] == ["北京南"], "stations come along"
    finally:
        core.shutdown()


def test_fewer_than_six_cells_use_overpass(home, hooks, overpass, geofabrik):
    overpass.routes["/api/interpreter"] = lambda r: (200, {"Content-Type": "application/json"}, overpass_json())
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    core.extracts.index_url = geofabrik.url + "/index-v1.json"
    try:
        core.cells.apply(SIX[:5], [], (116, 39))
        assert wait_for(lambda: all(core.store.cell(c)["status"] == "cached" for c in SIX[:5]), timeout=60)
        assert overpass.count("/api/interpreter") == 5 * 4
        assert geofabrik.count() == 0, "the extract index is not even read"
    finally:
        core.shutdown()


def test_extract_failure_falls_back_to_overpass(home, hooks, overpass, geofabrik):
    overpass.routes["/api/interpreter"] = lambda r: (200, {"Content-Type": "application/json"}, overpass_json())
    for path in [p for p in geofabrik.routes if p.endswith(".osm.pbf")]:
        geofabrik.routes[path] = lambda r: (503, {}, b"")
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    core.extracts.index_url = geofabrik.url + "/index-v1.json"
    try:
        core.cells.apply(SIX, [], (116, 39))
        assert wait_for(lambda: all(core.store.cell(c)["status"] == "cached" for c in SIX), timeout=90), [
            core.store.cell(c)["status"] for c in SIX
        ]
        assert overpass.count("/api/interpreter") == 6 * 4, "every cell came from Overpass in the end"
    finally:
        core.shutdown()


def test_a_few_cells_share_an_extract_being_downloaded(home, hooks, overpass, geofabrik):
    """Fewer than six cells use an extract that is being downloaded (here: for the
    construction overlay) instead of asking Overpass: nothing is downloaded twice."""
    import threading

    overpass.routes["/api/interpreter"] = lambda r: (200, {"Content-Type": "application/json"}, overpass_json())
    serve = geofabrik.routes["/tianjin.osm.pbf"]
    geofabrik.routes["/tianjin.osm.pbf"] = lambda r: (time.sleep(1.0 if r.method == "GET" else 0), serve(r))[1]
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    core.extracts.index_url = geofabrik.url + "/index-v1.json"
    try:
        overlay = threading.Thread(target=core.extracts.ensure, args=("tianjin", threading.Event(), lambda *a: None))
        overlay.start()
        assert wait_for(lambda: core.extracts.busy("tianjin"))
        core.cells.apply(["210-97"], [], (116, 39))  # in "tianjin"
        assert wait_for(lambda: core.store.cell("210-97")["status"] == "cached", timeout=60)
        overlay.join(10)
        assert overpass.count("/api/interpreter") == 0, "no Overpass queries"
        downloads = [r.path for r in geofabrik.requests if r.method == "GET" and r.path.endswith(".osm.pbf")]
        assert downloads == ["/tianjin.osm.pbf"]
    finally:
        core.shutdown()


def test_cells_and_the_construction_overlay_share_all_of_china(home, hooks, overpass, geofabrik):
    """Many cells chose all of China's extract while 「永远显示在建线路」 waited for them
    (SPEC §7.9): the overlay reads every province from it instead of downloading them."""
    overpass.routes["/api/interpreter"] = lambda r: (200, {"Content-Type": "application/json"}, overpass_json())
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    core.extracts.index_url = geofabrik.url + "/index-v1.json"
    try:
        core.cells.apply(SIX, [], (116, 39))
        assert wait_for(lambda: core.downloader.busy)
        core.settings.always_show_construction = True
        core.construction.settings_changed()
        assert wait_for(lambda: all(core.store.cell(c)["status"] == "cached" for c in SIX), timeout=60)
        assert wait_for(lambda: not core.construction._thread.is_alive(), timeout=30)
        downloads = [r.path for r in geofabrik.requests if r.method == "GET" and r.path.endswith(".osm.pbf")]
        assert downloads == ["/china.osm.pbf"], downloads
        assert core.construction._built_at("china") > 0, "the overlay read it"
        assert core.construction.question() is None, "nothing was downloaded for the overlay alone"
        # switching on again later downloads nothing either
        core.construction.settings_changed()
        assert wait_for(lambda: not core.construction._thread.is_alive(), timeout=30)
        assert len([r for r in geofabrik.requests if r.method == "GET" and r.path.endswith(".osm.pbf")]) == 1
    finally:
        core.shutdown()


def overlay_first_then_cells(home, hooks, overpass, geofabrik, sizes):
    """「永远显示在建线路」 is switched on first; while it downloads its first province
    (Beijing, in view), six cells are selected: it waits for them, then goes on
    (SPEC §7.9).  Returns once it is done (cells may still be at work) with the core,
    the times each extract was requested and the question it asked."""
    overpass.routes["/api/interpreter"] = lambda r: (200, {"Content-Type": "application/json"}, overpass_json())
    basemap = overpass.routes["/basemap.pmtiles"]
    overpass.routes["/basemap.pmtiles"] = lambda r: (time.sleep(1.0), basemap(r))[1]  # cells take a while
    got: dict[str, list[float]] = {}
    for path in [p for p in geofabrik.routes if p.endswith(".osm.pbf")]:
        def route(rec, serve=geofabrik.routes[path], name=path[1:-len(".osm.pbf")]):
            if rec.method == "GET":
                got.setdefault(name, []).append(time.time())
                if name == "beijing":
                    time.sleep(1.5)  # still downloading when the cells are selected
            return serve(rec)
        geofabrik.routes[path] = route
    core = make_core(home, hooks, overpass.url + "/api/interpreter")
    core.extracts.index_url = geofabrik.url + "/index-v1.json"
    core.extracts._sizes.update(sizes)  # what HEAD requests to Geofabrik would say
    asked: list[dict] = []
    publish = core.construction.on_question
    core.construction.on_question = lambda question: (asked.append(question), publish(question))
    core.settings.last_view = (116.4, 40.5, 6.0)  # in "beijing"
    core.settings.always_show_construction = True
    core.construction.settings_changed()
    assert wait_for(lambda: "beijing" in got)
    core.cells.apply(SIX, [], (116, 39))  # they need beijing, tianjin, east1 and west2
    assert wait_for(lambda: not core.construction._thread.is_alive(), timeout=90)
    return core, got, asked[-1] if asked else None


def all_cached(core) -> bool:
    return wait_for(lambda: all(core.store.cell(c)["status"] == "cached" for c in SIX), timeout=90)


def extracts_left(core) -> list[str]:
    return sorted(p.name.split(".")[0] for p in core.extracts.folder.glob("*.railways.sqlite"))


def test_overlay_first_then_cells_with_provinces(home, hooks, overpass, geofabrik):
    sizes = {"china": 3000 * 2**20, **{p: 50 * 2**20 for p in ("beijing", "tianjin", "east1", "west1", "west2", "east2")}}
    core, got, question = overlay_first_then_cells(home, hooks, overpass, geofabrik, sizes)
    try:
        assert all_cached(core)
        assert all(len(times) == 1 for times in got.values()), f"each extract downloaded once: {got}"
        assert set(got) == {"beijing", "tianjin", "east1", "west2", "east2", "west1"}, "every province, no China"
        done = max(core.store.cell(c)["rail_at"] for c in SIX)
        assert min(got["east2"] + got["west1"]) > done, "the overlay's own provinces only after the cells"
        assert sorted(question["regions"]) == ["east2", "west1"], "Beijing went to the cells too"
        result = core.construction.decide(True, question["regions"])
        assert result["deleted"] == 2 and result["kept"] == 0
        assert extracts_left(core) == ["beijing", "east1", "tianjin", "west2"], "the cells' extracts stay"
        assert core.construction.question() is None
    finally:
        core.shutdown()


def test_overlay_first_then_cells_with_all_of_china(home, hooks, overpass, geofabrik):
    sizes = {"china": 120 * 2**20, **{p: 50 * 2**20 for p in ("beijing", "tianjin", "east1", "west1", "west2", "east2")}}
    core, got, question = overlay_first_then_cells(home, hooks, overpass, geofabrik, sizes)
    try:
        assert core.store.pending_cells(), "the overlay was done before the cells (they still use China)"
        assert question["regions"] == ["beijing"], "downloaded only for the overlay: the cells read China"
        result = core.construction.decide(True, question["regions"])  # answered at once
        assert result["deleted"] == 1 and result["kept"] == 0
        assert all_cached(core)
        assert all(len(times) == 1 for times in got.values()), f"each extract downloaded once: {got}"
        assert set(got) == {"beijing", "china"}, "after Beijing, all of China for the cells and the overlay"
        assert core.construction._built_at("china") > 0
        assert extracts_left(core) == ["china"]
        assert core.construction.question() is None
    finally:
        core.shutdown()
