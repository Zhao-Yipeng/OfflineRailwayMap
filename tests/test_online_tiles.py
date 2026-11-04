from __future__ import annotations

import socket
import time

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage

from fakeserver import FakeServer
from offline_railway_map import USER_AGENT
from offline_railway_map.coverage import CellCoverage
from offline_railway_map.events import EventBus
from offline_railway_map.masking import TRANSPARENT_PNG
from offline_railway_map.net import Http, NetStatus
from offline_railway_map.online_tiles import CachedTile, OnlineSource, OnlineTiles, expiry_from_headers


def solid_png(color: str = "red", size: int = 256) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


RED = solid_png()


@pytest.fixture
def fake():
    server = FakeServer().start()
    yield server
    server.stop()


def make_tiles(home, fake_url: str, coverage: CellCoverage | None = None, limit_mb: int = 2048):
    sources = (
        OnlineSource("osm", "OSM 底图", fake_url + "/osm/{z}/{x}/{y}.png", 2.0, 0, 19),
        OnlineSource("orm", "ORM 瓦片", fake_url + "/orm/{z}/{x}/{y}.png", 2.0, 2, 19),
    )
    status = NetStatus(EventBus())
    tiles = OnlineTiles(home, Http(), status, coverage or CellCoverage(), lambda: limit_mb, sources)
    return tiles, status


def test_empty_tile_is_fully_transparent():
    image = QImage.fromData(TRANSPARENT_PNG)
    assert not image.isNull()
    assert image.pixelColor(0, 0).alpha() == 0


def test_tile_is_fetched_once_and_then_served_from_disk(home, fake):
    fake.routes["/orm/10/843/388.png"] = lambda r: (200, {"Content-Type": "image/png", "Cache-Control": "max-age=3600"}, RED)
    tiles, _ = make_tiles(home, fake.url)
    assert not home.data_exists()
    assert tiles.tile("orm", 10, 843, 388) == RED
    assert home.data_exists(), "the disk cache creates the data folder on first use"
    assert tiles.tile("orm", 10, 843, 388) == RED
    assert fake.count("/orm/") == 1
    assert fake.requests[0].headers["User-Agent"] == USER_AGENT
    assert "Referer" not in fake.requests[0].headers
    tiles.close()


def test_expired_tile_is_revalidated(home, fake):
    calls = []

    def reply(rec):
        calls.append(rec.headers.get("If-None-Match"))
        if rec.headers.get("If-None-Match") == '"v1"':
            return 304, {"Cache-Control": "max-age=3600", "ETag": '"v1"'}, b""
        return 200, {"Content-Type": "image/png", "Cache-Control": "max-age=0", "ETag": '"v1"'}, RED

    fake.routes["/osm/5/1/2.png"] = reply
    tiles, _ = make_tiles(home, fake.url)
    assert tiles.tile("osm", 5, 1, 2) == RED
    assert tiles.tile("osm", 5, 1, 2) == RED  # stale -> conditional request -> 304
    assert tiles.tile("osm", 5, 1, 2) == RED  # fresh again -> no request
    assert calls == [None, '"v1"']
    tiles.close()


def test_429_pauses_the_service(home, fake):
    fake.routes["/orm/9/1/1.png"] = lambda r: (429, {"Retry-After": "1"}, b"")
    tiles, status = make_tiles(home, fake.url)
    assert tiles.tile("orm", 9, 1, 1) == TRANSPARENT_PNG
    assert status.snapshot()["services"]["orm"]["state"] == "paused"
    assert tiles.tile("orm", 9, 1, 1) == TRANSPARENT_PNG
    assert fake.count("/orm/") == 1, "no further requests while paused"
    tiles.close()


def test_refusal_is_not_cached_and_reported(home, fake):
    fake.routes["/osm/3/1/1.png"] = lambda r: (
        200,
        {"Content-Type": "image/png", "x-blocked": "Access denied. See policy", "Cache-Control": "no-cache"},
        RED,
    )
    tiles, status = make_tiles(home, fake.url)
    assert tiles.tile("osm", 3, 1, 1) == TRANSPARENT_PNG
    service = status.snapshot()["services"]["osm"]
    assert service["state"] == "blocked"
    assert "拒绝访问" in service["message"]
    assert not home.data_exists(), "a refusal image must never be stored as a tile"
    tiles.close()


def test_stale_tile_is_used_when_the_network_is_down(home):
    with socket.socket() as s:  # a port with nobody listening
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    tiles, status = make_tiles(home, dead)
    tiles.cache.put("osm", 4, 3, 2, CachedTile(RED, None, None, time.time() - 10))
    for _ in range(3):
        assert tiles.tile("osm", 4, 3, 2) == RED
    assert status.snapshot()["network"] == "offline"
    tiles.close()


def test_tiles_inside_cached_cells_are_never_requested(home, fake):
    fake.default = lambda r: (200, {"Content-Type": "image/png"}, RED)
    coverage = CellCoverage({(210, 97)})
    tiles, _ = make_tiles(home, fake.url, coverage)
    assert tiles.tile("orm", 8, 210, 97) == TRANSPARENT_PNG
    assert tiles.tile("orm", 12, 210 * 16 + 5, 97 * 16 + 9) == TRANSPARENT_PNG
    assert fake.count() == 0
    assert tiles.tile("orm", 8, 211, 97) == RED
    tiles.close()


def test_partially_cached_tile_is_masked(home, fake):
    fake.default = lambda r: (200, {"Content-Type": "image/png"}, RED)
    # z7 tile (105, 48) contains cells x 210-211, y 96-97; cache the bottom-left one.
    coverage = CellCoverage({(210, 97)})
    tiles, _ = make_tiles(home, fake.url, coverage)
    image = QImage.fromData(tiles.tile("orm", 7, 105, 48))
    assert image.width() == 256
    assert image.pixelColor(64, 192).alpha() == 0, "bottom-left quadrant is cached -> transparent"
    assert image.pixelColor(128, 191).alpha() == 255
    assert image.pixelColor(192, 64).alpha() == 255
    assert image.pixelColor(127, 128).alpha() == 0
    tiles.close()


def test_cache_is_trimmed_to_its_limit(home, fake):
    tiles, _ = make_tiles(home, fake.url, limit_mb=1)
    blob = b"x" * 300_000
    for i in range(10):
        tiles.cache.put("osm", 10, i, 0, CachedTile(blob, None, None, time.time() + 100))
    assert tiles.cache.total_bytes <= 1024 * 1024
    assert tiles.cache.get("osm", 10, 9, 0) is not None
    tiles.close()


def test_memory_only_mode_after_clearing(home, fake):
    fake.routes["/orm/6/1/1.png"] = lambda r: (200, {"Content-Type": "image/png", "Cache-Control": "max-age=600"}, RED)
    tiles, _ = make_tiles(home, fake.url)
    tiles.cache.memory_only = True
    assert tiles.tile("orm", 6, 1, 1) == RED
    assert tiles.tile("orm", 6, 1, 1) == RED
    assert fake.count("/orm/") == 1, "still cached, but only in memory"
    assert not home.data_exists(), "the data folder is not recreated"
    home.ensure_data()  # e.g. the user starts a download
    assert tiles.cache.memory_only is False
    tiles.close()


def test_out_of_range_tiles_are_empty(home, fake):
    tiles, _ = make_tiles(home, fake.url)
    assert tiles.tile("orm", 1, 0, 0) == TRANSPARENT_PNG  # below ORM's min zoom
    assert tiles.tile("osm", 3, 8, 0) == TRANSPARENT_PNG
    assert fake.count() == 0
    tiles.close()


@pytest.mark.parametrize(
    "headers, expected_offset, storable",
    [
        ({"Cache-Control": "max-age=600"}, 600, True),
        ({"Cache-Control": "public, max-age=15238"}, 15238, True),
        ({"Cache-Control": "no-cache"}, 0, True),
        ({"Cache-Control": "no-store"}, 0, False),
        ({}, 7 * 24 * 3600, True),
    ],
)
def test_expiry_rules(headers, expected_offset, storable):
    now = 1_000_000.0
    expires, can_store = expiry_from_headers(headers, now)
    assert expires == now + expected_offset
    assert can_store is storable


def test_expires_header_is_used():
    expires, _ = expiry_from_headers({"Expires": "Thu, 05 Nov 2026 00:48:08 GMT"}, 0)
    assert expires == 1793839688.0


def test_coverage_masks():
    coverage = CellCoverage({(210, 97), (211, 97)})
    assert coverage.tile_mask(8, 210, 97).full
    assert coverage.tile_mask(14, 210 * 64 + 63, 97 * 64).full
    assert coverage.tile_mask(9, 2 * 212, 2 * 97) is None
    mask = coverage.tile_mask(7, 105, 48)
    assert not mask.full
    assert sorted(mask.rects) == [(0.0, 0.5, 0.5, 1.0), (0.5, 0.5, 1.0, 1.0)]
    coverage.replace({(210, 96), (211, 96), (210, 97), (211, 97)})
    assert coverage.tile_mask(7, 105, 48).full
    assert CellCoverage().tile_mask(3, 1, 1) is None
