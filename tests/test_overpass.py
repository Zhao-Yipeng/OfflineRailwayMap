from __future__ import annotations

import json
import re
import socket
import threading
from urllib.parse import parse_qs

import pytest
import requests

from fakeserver import FakeServer
from offline_railway_map import USER_AGENT
from offline_railway_map.grid import tile_bounds
from offline_railway_map.overpass import (
    BudgetExhausted,
    Cancelled,
    Offline,
    OverpassClient,
    QueryFailed,
    ServersBusy,
    build_query,
)
from offline_railway_map.store import StateStore


def reply(elements):
    return 200, {"Content-Type": "application/json"}, json.dumps({"elements": elements}).encode()


def bbox_of(rec) -> str:
    query = parse_qs(rec.body.decode())["data"][0]
    return re.search(r"\]\(([^)]*)\);", query).group(1)


def whole_cell(rec, x=210, y=97) -> bool:
    west, south, east, north = tile_bounds(8, x, y)
    return bbox_of(rec) == f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"


@pytest.fixture
def fake():
    server = FakeServer().start()
    yield server
    server.stop()


def make_client(home, endpoints, *, busy_seconds=0.0):
    """busy_seconds=0 makes every 5xx count as "area too heavy" (no busy wait)."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    store = StateStore(home)
    client = OverpassClient(
        session, lambda: endpoints, store, min_interval=0, default_retry_after=0.01,
        busy_backoff=(0.01, 0.01), busy_response_seconds=busy_seconds,
    )
    return client, store


def dead_endpoint() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}/api/interpreter"


def test_query_text_matches_spec():
    q = build_query((115.3125, 38.8226, 116.71875, 39.9097))
    assert "(38.8226000,115.3125000,39.9097000,116.7187500)" in q
    assert 'way["railway"~"^(rail|narrow_gauge|subway|light_rail|tram|construction|proposed|disused)$"]' in q
    assert "out tags geom;" in q and "out tags center;" in q


def test_simple_download_records_usage(home, fake):
    fake.routes["/api/interpreter"] = lambda r: reply([{"type": "way", "id": 1}])
    client, store = make_client(home, [fake.url + "/api/interpreter"])
    progress = []
    elements = client.fetch_cell(210, 97, threading.Event(), progress.append)
    assert elements == [{"type": "way", "id": 1}]
    assert progress[-1] == 1.0
    assert fake.requests[0].headers["User-Agent"] == USER_AGENT
    requests_today, bytes_today = store.usage_today(fake.url + "/api/interpreter")
    assert requests_today == 1 and bytes_today > 0
    store.close()


def test_429_is_retried_on_the_same_server(home, fake):
    calls = []

    def route(rec):
        calls.append(1)
        if len(calls) < 3:
            return 429, {"Retry-After": "0"}, b""
        return reply([{"type": "node", "id": 5}])

    fake.routes["/api/interpreter"] = route
    client, store = make_client(home, [fake.url + "/api/interpreter"])
    assert client.fetch_cell(1, 1, threading.Event(), lambda p: None) == [{"type": "node", "id": 5}]
    assert len(calls) == 3
    store.close()


def test_overloaded_server_leads_to_split_and_dedup(home, fake):
    def route(rec):
        # the whole cell is "too heavy"; the z9 quarters work and overlap on element 1
        if whole_cell(rec):
            return 504, {}, b""
        return reply([{"type": "way", "id": 1}, {"type": "way", "id": hash(bbox_of(rec)) % 1000 + 10}])

    fake.routes["/a"] = route
    fake.routes["/b"] = route
    client, store = make_client(home, [fake.url + "/a", fake.url + "/b"])
    progress = []
    elements = client.fetch_cell(210, 97, threading.Event(), progress.append)
    ids = sorted(e["id"] for e in elements)
    assert ids.count(1) == 1, "elements returned by several parts are kept once"
    assert len(fake.requests) == 2 + 4, "two servers try the whole cell, then four quarters"
    assert progress[-1] == pytest.approx(1.0)
    store.close()


def test_runtime_error_remark_counts_as_overload(home, fake):
    def route(rec):
        if whole_cell(rec):
            return 200, {}, json.dumps({"elements": [], "remark": "runtime error: Query run out of memory"}).encode()
        return reply([])

    fake.routes["/a"] = route
    client, store = make_client(home, [fake.url + "/a"])
    client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    assert len(fake.requests) == 1 + 4
    store.close()


def test_unreachable_first_server_falls_back(home, fake):
    fake.routes["/api/interpreter"] = lambda r: reply([{"type": "way", "id": 9}])
    client, store = make_client(home, [dead_endpoint(), fake.url + "/api/interpreter"])
    assert client.fetch_cell(3, 3, threading.Event(), lambda p: None) == [{"type": "way", "id": 9}]
    store.close()


def test_all_servers_unreachable_is_offline(home):
    client, store = make_client(home, [dead_endpoint(), dead_endpoint()])
    with pytest.raises(Offline):
        client.fetch_cell(3, 3, threading.Event(), lambda p: None)
    store.close()


def test_area_that_never_works_fails(home, fake):
    fake.routes["/a"] = lambda r: (504, {}, b"")
    client, store = make_client(home, [fake.url + "/a"])
    with pytest.raises(QueryFailed):
        client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    # depth first: whole cell (z8) -> first quarter (z9) -> its first quarter (z10) -> give up
    assert len(fake.requests) == 3
    store.close()


def test_busy_servers_are_retried_then_reported(home, fake):
    fake.routes["/a"] = lambda r: (504, {}, b"busy")
    fake.routes["/b"] = lambda r: (503, {}, b"busy")
    client, store = make_client(home, [fake.url + "/a", fake.url + "/b"], busy_seconds=30)
    with pytest.raises(ServersBusy):
        client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    # three rounds over both servers, never split into smaller areas
    assert len(fake.requests) == 6
    assert all(whole_cell(r) for r in fake.requests)
    store.close()


def test_busy_server_recovers(home, fake):
    calls = []

    def route(rec):
        calls.append(1)
        return (504, {}, b"busy") if len(calls) < 3 else reply([{"type": "way", "id": 4}])

    fake.routes["/a"] = route
    client, store = make_client(home, [fake.url + "/a"], busy_seconds=30)
    assert client.fetch_cell(1, 1, threading.Event(), lambda p: None) == [{"type": "way", "id": 4}]
    store.close()


def test_unreachable_server_is_tried_last_afterwards(home, fake):
    fake.routes["/ok"] = lambda r: reply([])
    dead = dead_endpoint()
    client, store = make_client(home, [dead, fake.url + "/ok"])
    client.fetch_cell(1, 1, threading.Event(), lambda p: None)
    assert client._ordered_endpoints() == [fake.url + "/ok", dead]
    store.close()


def test_daily_budget(home, fake):
    fake.routes["/a"] = lambda r: reply([])
    client, store = make_client(home, [fake.url + "/a"])
    store.add_usage(fake.url + "/a", 9000, 0)
    with pytest.raises(BudgetExhausted):
        client.fetch_cell(1, 1, threading.Event(), lambda p: None)
    assert fake.requests == []
    store.close()


def test_cancel(home, fake):
    fake.routes["/a"] = lambda r: reply([])
    client, store = make_client(home, [fake.url + "/a"])
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        client.fetch_cell(1, 1, cancel, lambda p: None)
    store.close()
