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


def test_parts_of_a_refused_size_are_split_without_asking(home, fake):
    def route(rec):
        south, west, north, east = (float(v) for v in bbox_of(rec).split(","))
        # areas wider than a z10 tile (0.35 degrees) are refused
        return (504, {}, b"") if east - west > 0.6 else reply([{"type": "way", "id": 1}])

    fake.routes["/a"] = route
    client, store = make_client(home, [fake.url + "/a"])
    client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    # z8 refused, first z9 refused, then all 16 z10 parts; the other three z9
    # parts are split straight away instead of being tried first
    assert len(fake.requests) == 1 + 1 + 16
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


def test_area_that_never_works_goes_back_to_the_queue(home, fake):
    fake.routes["/a"] = lambda r: (504, {}, b"")
    client, store = make_client(home, [fake.url + "/a"])
    with pytest.raises(ServersBusy):
        client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    # depth first: whole cell (z8) -> first quarter (z9) -> z10 -> z11; the
    # smallest part is retried (three rounds) and then left for later
    assert len(fake.requests) == 1 + 1 + 1 + 3
    store.close()


def test_retry_continues_where_it_stopped(home, fake):
    # one railway in the middle of each z11 part of the cell, like real data
    railways = {}
    for tx in range(210 * 8, 211 * 8):
        for ty in range(97 * 8, 98 * 8):
            west, south, east, north = tile_bounds(11, tx, ty)
            railways[len(railways)] = ((west + east) / 2, (south + north) / 2)
    state = {"broken": True}
    answered = []

    def route(rec):
        south, west, north, east = (float(v) for v in bbox_of(rec).split(","))
        if east - west > 0.6 or (state["broken"] and west > 116.0):
            return 504, {}, b""
        answered.append(bbox_of(rec))
        inside = [i for i, (lon, lat) in railways.items() if west <= lon <= east and south <= lat <= north]
        return reply([{"type": "way", "id": i} for i in inside])

    fake.routes["/a"] = route
    client, store = make_client(home, [fake.url + "/a"])
    with pytest.raises(ServersBusy):
        client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    first = set(answered)
    assert first, "some parts were downloaded before the servers gave up"
    before = len(fake.requests)
    state["broken"] = False
    elements = client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    assert sorted(e["id"] for e in elements) == list(range(64)), "the whole cell, every railway once"
    again = {bbox_of(r) for r in fake.requests[before:]}
    assert not again & first, "parts downloaded the first time are not fetched again"
    assert client._progress == {}, "nothing is kept once the cell is complete"
    store.close()


def test_only_the_current_cell_is_remembered(home, fake):
    fake.routes["/a"] = lambda r: (504, {}, b"") if whole_cell(r) else reply([{"type": "way", "id": 1}])
    fake.routes["/b"] = lambda r: (504, {}, b"")
    endpoints = [fake.url + "/a"]
    client, store = make_client(home, endpoints)
    client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    endpoints[0] = fake.url + "/b"
    with pytest.raises(ServersBusy):
        client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    assert list(client._progress) == [(210, 97)]
    with pytest.raises(ServersBusy):
        client.fetch_cell(211, 97, threading.Event(), lambda p: None)
    assert list(client._progress) == [(211, 97)], "the previous cell's parts are dropped"
    client.forget_cell(211, 97)
    assert client._progress == {}
    store.close()


def test_rejected_query_fails(home, fake):
    fake.routes["/a"] = lambda r: (400, {}, b"syntax error")
    client, store = make_client(home, [fake.url + "/a"])
    with pytest.raises(QueryFailed):
        client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    store.close()


def test_busy_servers_are_retried_then_reported(home, fake):
    fake.routes["/a"] = lambda r: (504, {}, b"busy")
    fake.routes["/b"] = lambda r: (503, {}, b"busy")
    client, store = make_client(home, [fake.url + "/a", fake.url + "/b"], busy_seconds=30)
    with pytest.raises(ServersBusy):
        client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    # z8, z9, z10: two busy rounds over both servers, then split;
    # z11 (smallest part): three rounds, then left for later
    assert len(fake.requests) == 4 + 4 + 4 + 6
    assert sum(whole_cell(r) for r in fake.requests) == 4
    store.close()


def test_busy_server_recovers(home, fake):
    calls = []

    def route(rec):
        calls.append(1)
        return (504, {}, b"busy") if len(calls) < 2 else reply([{"type": "way", "id": 4}])

    fake.routes["/a"] = route
    client, store = make_client(home, [fake.url + "/a"], busy_seconds=30)
    assert client.fetch_cell(1, 1, threading.Event(), lambda p: None) == [{"type": "way", "id": 4}]
    assert len(calls) == 2, "one busy answer, then the same area succeeds without splitting"
    store.close()


def test_area_refused_as_a_whole_is_split(home, fake):
    """mail.ru-like behaviour: large areas get a fast 504, small ones work."""
    fake.routes["/a"] = lambda r: (504, {}, b"busy") if whole_cell(r) else reply([{"type": "way", "id": 7}])
    client, store = make_client(home, [fake.url + "/a"], busy_seconds=30)
    assert client.fetch_cell(210, 97, threading.Event(), lambda p: None) == [{"type": "way", "id": 7}]
    assert len(fake.requests) == 2 + 4
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
