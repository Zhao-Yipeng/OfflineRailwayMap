"""「清空缓存」 after long use leaves exactly the unpacked program folder (SPEC §6.6, §11).

Several program runs (each a fresh ``Home`` and ``AppCore``, like a restart)
download cells, browse online and offline tiles, change settings, search and
leave crash leftovers behind.  Then 「清空缓存」 runs while other threads keep
asking for tiles and data, as the page does.  Afterwards the program folder
must hold exactly the files it had when it was unpacked, and nothing may have
been written to the system's temporary folder.  On Windows (CI) this also
checks that every file was closed: Windows cannot delete open files.
"""

from __future__ import annotations

import http.client
import tempfile
import threading
import time
from pathlib import Path

import pytest

from conftest import Client, RecordingHooks
from fakeserver import FakeServer
from offline_railway_map import cleanup, logs
from offline_railway_map.core import AppCore
from offline_railway_map.online_tiles import OnlineSource
from offline_railway_map.paths import Home
from offline_railway_map.settings import Settings
from pmtiles_writer import range_route, write_archive
from railfixtures import overpass_json
from test_basemap import cell_tiles
from test_online_tiles import RED

MVT = b"\x1a\x05fake!"
PROGRAM_FILES = {
    "OfflineRailwayMap.exe": b"MZ" + b"\0" * 4000,
    "_internal/python312.dll": b"\0" * 8000,
    "_internal/offline_railway_map/web/index.html": b"<!doctype html>",
    "README.md": b"# OfflineRailwayMap\n",
    "LICENSE": b"GPL-3.0\n",
}


def snapshot(root: Path) -> dict[str, int]:
    """Every file and folder below ``root`` (folders as -1)."""
    return {str(p.relative_to(root)): (p.stat().st_size if p.is_file() else -1) for p in sorted(root.rglob("*"))}


@pytest.fixture
def servers():
    """Overpass, Protomaps, OpenFreeMap and ORM stand-ins in one server."""
    server = FakeServer().start()
    server.routes["/api/interpreter"] = lambda r: (200, {"Content-Type": "application/json"}, overpass_json())
    server.routes["/basemap.pmtiles"] = range_route(write_archive(cell_tiles(max_zoom=10), max_zoom=10))
    server.routes["/planet"] = lambda r: (
        200, {"Content-Type": "application/json"}, ('{"tiles": ["%s/v1/{z}/{x}/{y}.pbf"]}' % server.url).encode(),
    )

    def route(rec):
        if rec.path.startswith("/v1/"):
            return 200, {"Content-Type": "application/x-protobuf", "Cache-Control": "max-age=315360000"}, MVT
        if rec.path.startswith("/orm/"):
            return 200, {"Content-Type": "image/png", "Cache-Control": "max-age=86400"}, RED
        return 404, {}, b""

    server.default = route
    yield server
    server.stop()


class Run:
    """One start of the program (what app.main does, without the window)."""

    def __init__(self, root: Path, servers: FakeServer) -> None:
        self.home = Home(root)
        cleanup.finish_pending_clear(self.home)
        self.home.on_data_created(lambda: logs.attach_file(self.home))
        if self.home.data_exists():
            logs.attach_file(self.home)
        cleanup.remove_tmp(self.home)
        settings = Settings.load(self.home)
        settings.overpass_endpoints = [servers.url + "/api/interpreter"]
        settings.basemap_source = servers.url + "/basemap.pmtiles"
        sources = (
            OnlineSource("ofm", "在线底图", "", 5.0, 0, 14, kind="vector", tilejson=servers.url + "/planet", max_ttl=7 * 86400),
            OnlineSource("orm", "ORM 瓦片", servers.url + "/orm/{z}/{x}/{y}.png", 5.0, 2, 19),
        )
        self.core = AppCore(self.home, settings, hooks=RecordingHooks(), online_sources=sources)
        self.core.overpass.min_interval = 0
        self.core.start()
        self.client = Client(self.core)

    def get(self, path: str, online: bool = False) -> int:
        server = self.core.online_server if online else self.core.server
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        conn.request("GET", f"/{server.token}{path}", headers={"Host": f"127.0.0.1:{server.port}"})
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status

    def browse(self) -> None:
        """What looking around the map asks for: every kind of tile, plus data."""
        x, y = 210, 97
        for z in range(2, 15):
            s = z - 8
            tx, ty = (x << s, y << s) if s >= 0 else (x >> -s, y >> -s)
            assert self.get(f"/tiles/rail/{z}/{tx}/{ty}.mvt?v=1") in (200, 204)
            assert self.get(f"/tiles/basemap/{z}/{tx}/{ty}.mvt?v=1") in (200, 204)
            assert self.get(f"/tiles/ofm/{min(z, 14)}/{tx + 1}/{ty}.pbf?v=1", online=True) in (200, 204)
            assert self.get(f"/tiles/orm/{z}/{tx + 1}/{ty}.png?v=1", online=True) == 200
            assert self.get(f"/tiles/construction/{z}/{tx + 1}/{ty}.mvt?v=1") in (200, 204)
        assert self.get("/api/cells") == 200
        assert self.get("/api/search?q=%E7%AB%99") == 200  # 站
        assert self.get("/api/details?cell=210-97&way=1") in (200, 404)

    def wait_for_construction_lines(self) -> None:
        """「永远显示在建线路」: the overlay's data is fetched and kept in data\\cache."""
        end = time.time() + 30
        while time.time() < end:
            if self.get("/tiles/construction/6/52/24.mvt?v=1") == 200:  # z6: not masked by cached cells
                assert (self.home.cache_dir / "construction.sqlite").exists()
                return
            time.sleep(0.1)
        raise AssertionError("no construction lines")

    def download(self, cid: str) -> None:
        status, _ = self.client.post_json("/api/cells/apply", {"add": [cid], "remove": []})
        assert status == 200
        end = time.time() + 60
        while time.time() < end:
            row = self.core.store.cell(cid)
            if row and row["status"] == "cached":
                return
            time.sleep(0.05)
        raise AssertionError(f"{cid} was not downloaded: {self.core.store.cell(cid)}")

    def stop(self) -> None:
        self.core.shutdown()
        logs.detach_file()


def test_clearing_after_long_use_leaves_only_the_program(tmp_path, servers, monkeypatch):
    root = tmp_path / "OfflineRailwayMap"
    for name, data in PROGRAM_FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    unpacked = snapshot(root)
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))

    # Run 1: just opening and closing the program writes nothing.
    run = Run(root, servers)
    run.stop()
    assert snapshot(root) == unpacked

    # Run 2: browse, change a setting, download a cell, look at it.
    run = Run(root, servers)
    run.browse()
    assert run.client.post_json("/api/settings", {"showCachedOutline": False, "alwaysShowConstruction": True})[0] == 200
    run.download("210-97")
    run.browse()
    run.wait_for_construction_lines()
    assert run.client.post_json("/api/view", {"lon": 116.4, "lat": 39.9, "zoom": 9})[0] == 200
    run.stop()
    assert (root / "data").is_dir()

    # Run 3, after a crash: leftovers of an interrupted download and install.
    (root / "data" / "tmp" / "211-97").mkdir(parents=True)
    (root / "data" / "tmp" / "211-97" / "rail.sqlite").write_bytes(b"half written")
    (root / "data" / "cells" / "210-97.old").mkdir()
    (root / "data" / "cells" / "210-97.old" / "rail.sqlite").write_bytes(b"old copy")
    run = Run(root, servers)
    run.download("211-97")
    run.browse()
    run.stop()

    # Run 4: clear the cache while the page keeps asking for tiles and data.
    run = Run(root, servers)
    run.browse()
    stop = threading.Event()
    errors: list[str] = []

    def keep_browsing() -> None:
        while not stop.is_set():
            try:
                run.browse()
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(repr(exc))

    threads = [threading.Thread(target=keep_browsing) for _ in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    status, result = run.client.post_json("/api/clear", {})
    assert status == 200 and result == {"ok": True, "failed": []}, result
    assert not (root / "data").exists(), "the data folder is gone"
    time.sleep(1.0)  # the page goes on looking around after clearing
    assert not (root / "data").exists(), "nothing recreated it"
    stop.set()
    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive(), "the program kept answering requests"
    assert not errors, errors[:3]
    run.stop()
    assert snapshot(root) == unpacked, "the program folder is exactly as it was unpacked"

    # Run 5: starting again after clearing still writes nothing.
    run = Run(root, servers)
    run.stop()
    assert snapshot(root) == unpacked
    assert list(system_temp.iterdir()) == [], "nothing in the system's temporary folder"
