from __future__ import annotations

import pytest

from offline_railway_map.cells import CellService
from offline_railway_map.coverage import CellCoverage
from offline_railway_map.events import EventBus
from offline_railway_map.grid import (
    cell_at,
    cell_bounds,
    cell_center,
    cell_size_km,
    parse_cell_id,
)
from offline_railway_map.store import StateStore


@pytest.fixture
def service(home):
    svc = CellService(home, StateStore(home), CellCoverage(), EventBus())
    svc.start()
    yield svc
    svc.store.close()


def test_grid_math():
    assert cell_at(116.4, 39.9) == (210, 97)  # Beijing (south half) -> SPEC appendix E sample
    assert cell_at(121.47, 31.23) == (214, 104)
    west, south, east, north = cell_bounds(210, 97)
    assert (round(west, 4), round(east, 4)) == (115.3125, 116.7188)
    assert round(north, 4) == 39.9097 and round(south, 4) == 38.8226
    lon, lat = cell_center(210, 97)
    assert west < lon < east and south < lat < north
    assert round(cell_size_km(128), 1) == 156.5  # row just south of the equator
    assert 118 < cell_size_km(97) < 122  # ~40 degrees north
    assert parse_cell_id("0-255") == (0, 255)
    for bad in ("256-1", "a-b", "1_2", "-1-2"):
        with pytest.raises(ValueError):
            parse_cell_id(bad)


def test_nothing_is_written_until_cells_are_added(home, service):
    assert service.snapshot()["cells"] == []
    service.apply([], [], None)
    service.delete(["1-2"])
    service.set_paused(False)
    assert not home.data_exists()


def test_cells_are_queued_nearest_first(home, service):
    result = service.apply(["220-90", "210-97", "211-97"], [], (116.4, 39.9))
    assert sorted(result["queued"]) == ["210-97", "211-97", "220-90"]
    assert home.state_db.exists()
    order = [c["id"] for c in service.snapshot()["cells"]]
    assert order == ["210-97", "211-97", "220-90"]
    assert all(c["status"] == "queued" for c in service.snapshot()["cells"])
    assert service.store.next_job()["id"] == "210-97"
    again = service.apply(["210-97"], [], (0, 0))
    assert again["queued"] == [], "already queued cells are not queued twice"


def test_deleting_a_cached_cell_removes_files_and_coverage(home, service):
    service.apply(["210-97"], [], None)
    service.store.update_cell("210-97", status="cached", rail_bytes=10)
    service.sync_coverage()
    assert service.coverage.is_cached(210, 97)
    version = service.tiles_version
    cell_dir = home.cell_dir("210-97")
    cell_dir.mkdir(parents=True)
    (cell_dir / "rail.sqlite").write_bytes(b"x")
    deleted_events = []
    service.on_delete(deleted_events.append)
    assert service.delete(["210-97"]) == ["210-97"]
    assert not cell_dir.exists()
    assert not service.coverage.is_cached(210, 97)
    assert service.tiles_version == version + 1
    assert deleted_events == [["210-97"]]


def test_apply_removes_and_adds_in_one_step(service):
    service.apply(["1-1", "2-2"], [], None)
    service.store.update_cell("1-1", status="cached")
    result = service.apply(["3-3"], ["1-1"], None)
    assert result["removed"] == ["1-1"]
    assert {c["id"] for c in service.snapshot()["cells"]} == {"2-2", "3-3"}


def test_retry_and_update(service):
    service.apply(["5-5", "6-6"], [], None)
    service.store.update_cell("5-5", status="failed", error="boom")
    service.store.update_cell("6-6", status="cached")
    assert service.retry(["5-5", "6-6"]) == ["5-5"]
    assert service.store.cell("5-5")["status"] == "queued"
    assert service.store.cell("5-5")["error"] is None
    assert service.request_update("6-6", with_basemap=True)
    row = service.store.cell("6-6")
    assert (row["status"], row["update_kind"]) == ("updating", "all")
    assert not service.request_update("5-5", with_basemap=False), "only cached cells can be updated"


def test_interrupted_downloads_return_to_the_queue(home, service):
    service.apply(["7-7"], [], None)
    service.store.update_cell("7-7", status="downloading", stage="rail", progress=0.5)
    fresh = CellService(home, StateStore(home), CellCoverage(), EventBus())
    fresh.start()
    row = fresh.store.cell("7-7")
    assert (row["status"], row["progress"], row["stage"]) == ("queued", 0, None)
    fresh.store.close()


def test_pause_flag(service):
    service.apply(["8-8"], [], None)
    service.set_paused(True)
    assert service.snapshot()["paused"] is True
    service.set_paused(False)
    assert service.snapshot()["paused"] is False


def test_cells_api(client):
    status, body = client.post_json("/api/cells/apply", {"add": ["210-97"], "remove": [], "center": {"lon": 116, "lat": 39}})
    assert status == 200
    assert [c["id"] for c in body["cells"]] == ["210-97"]
    assert client.post_json("/api/cells/apply", {"add": ["999-1"], "remove": []})[0] == 400
    assert client.post_json("/api/cells/apply", {"add": "210-97"})[0] == 400
    assert client.post_json("/api/cells/update", {"id": "210-97"})[0] == 409
    status, body = client.get_json("/api/cells")
    assert body["cells"][0]["status"] == "queued"
    assert client.post_json("/api/cells/delete", {"ids": ["210-97"]}) == (200, {"removed": ["210-97"]})
