from __future__ import annotations

import json
import re
import socket
import threading
import time
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
    slot_wait,
    status_url,
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


def width(rec) -> float:
    south, west, north, east = (float(v) for v in bbox_of(rec).split(","))
    return east - west


def quarter(rec) -> bool:
    """A z9 part of a cell (0.70 degrees wide); z10 parts are 0.35."""
    return width(rec) > 0.6


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
        busy_backoff=(0.01, 0.01), busy_response_seconds=busy_seconds, server_slots=lambda e: 1,
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
    assert "maxsize" not in q, "the server default; a big reservation gets refused when busy"


def test_a_cell_is_fetched_as_four_quarters(home, fake):
    fake.routes["/a"] = lambda r: reply([{"type": "way", "id": 1}])
    client, store = make_client(home, [fake.url + "/a"])
    progress = []
    client.fetch_cell(210, 97, threading.Event(), progress.append)
    assert len(fake.requests) == 4 and all(quarter(r) for r in fake.requests)
    assert not any(whole_cell(r) for r in fake.requests)
    assert progress == [0.25, 0.5, 0.75, 1.0]
    store.close()


def test_simple_download_records_usage(home, fake):
    fake.routes["/api/interpreter"] = lambda r: reply([{"type": "way", "id": 1}])
    client, store = make_client(home, [fake.url + "/api/interpreter"])
    progress = []
    elements = client.fetch_cell(210, 97, threading.Event(), progress.append)
    assert elements == [{"type": "way", "id": 1}], "the same way in several parts is kept once"
    assert progress[-1] == 1.0
    assert fake.requests[0].headers["User-Agent"] == USER_AGENT
    requests_today, bytes_today = store.usage_today(fake.url + "/api/interpreter")
    assert requests_today == 4 and bytes_today > 0
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
    assert len(calls) == 3 + 3, "two 429s on the first part, then four answers"
    store.close()


def test_overloaded_server_leads_to_split_and_dedup(home, fake):
    def route(rec):
        # quarters are "too heavy"; the z10 parts work and overlap on element 1
        if quarter(rec):
            return 504, {}, b""
        return reply([{"type": "way", "id": 1}, {"type": "way", "id": hash(bbox_of(rec)) % 1000 + 10}])

    fake.routes["/a"] = route
    fake.routes["/b"] = route
    client, store = make_client(home, [fake.url + "/a", fake.url + "/b"])
    progress = []
    elements = client.fetch_cell(210, 97, threading.Event(), progress.append)
    ids = sorted(e["id"] for e in elements)
    assert ids.count(1) == 1, "elements returned by several parts are kept once"
    assert len(fake.requests) == 2 + 16, "two servers try the first quarter, then 16 z10 parts"
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
    # the first z9 part is refused, then all 16 z10 parts; the other three z9
    # parts are split straight away instead of being tried first
    assert len(fake.requests) == 1 + 16
    store.close()


def test_runtime_error_remark_counts_as_overload(home, fake):
    def route(rec):
        if quarter(rec):
            return 200, {}, json.dumps({"elements": [], "remark": "runtime error: Query run out of memory"}).encode()
        return reply([])

    fake.routes["/a"] = route
    client, store = make_client(home, [fake.url + "/a"])
    client.fetch_cell(210, 97, threading.Event(), lambda p: None)
    assert len(fake.requests) == 1 + 16
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
    # depth first: first quarter (z9) -> z10 -> z11; the smallest part is
    # retried (three rounds) and then left for later
    assert len(fake.requests) == 1 + 1 + 3
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


def test_partly_downloaded_cells_are_remembered_up_to_a_limit(home, fake, monkeypatch):
    import offline_railway_map.overpass as overpass_module

    monkeypatch.setattr(overpass_module, "REMEMBERED_CELLS", 2)
    fake.routes["/b"] = lambda r: (504, {}, b"")
    client, store = make_client(home, [fake.url + "/b"])
    for x in (210, 211, 212):
        with pytest.raises(ServersBusy):
            client.fetch_cell(x, 97, threading.Event(), lambda p: None)
    assert list(client._progress) == [(211, 97), (212, 97)], "the oldest cell's parts are dropped"
    client.forget_cell(212, 97)
    assert list(client._progress) == [(211, 97)]
    client.forget_all()
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
    # z9, z10: two busy rounds over both servers, then split;
    # z11 (smallest part): three rounds, then left for later
    assert len(fake.requests) == 4 + 4 + 6
    assert sum(quarter(r) for r in fake.requests) == 4
    store.close()


def test_busy_server_recovers(home, fake):
    calls = []

    def route(rec):
        calls.append(1)
        return (504, {}, b"busy") if len(calls) < 2 else reply([{"type": "way", "id": 4}])

    fake.routes["/a"] = route
    client, store = make_client(home, [fake.url + "/a"], busy_seconds=30)
    assert client.fetch_cell(1, 1, threading.Event(), lambda p: None) == [{"type": "way", "id": 4}]
    assert len(calls) == 2 + 3, "one busy answer, then the same part succeeds without splitting"
    store.close()


def test_area_refused_as_a_whole_is_split(home, fake):
    """mail.ru-like behaviour: large areas get a fast 504, small ones work."""
    fake.routes["/a"] = lambda r: (504, {}, b"busy") if quarter(r) else reply([{"type": "way", "id": 7}])
    client, store = make_client(home, [fake.url + "/a"], busy_seconds=30)
    assert client.fetch_cell(210, 97, threading.Event(), lambda p: None) == [{"type": "way", "id": 7}]
    assert len(fake.requests) == 2 + 16, "two busy rounds for the first quarter, then z10 parts"
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


# -- rate limits, server order and progress reports ------------------------------------------


def test_slot_wait_is_read_from_the_status_page():
    assert status_url("https://overpass-api.de/api/interpreter") == "https://overpass-api.de/api/status"
    assert status_url("https://example.org/query") is None
    assert slot_wait("Connected as: 1\nRate limit: 2\n2 slots available now.\n") == 0.0
    page = (
        "Connected as: 1\nRate limit: 2\n"
        "Slot available after: 2026-11-05T02:00:30Z, in 23 seconds.\n"
        "Slot available after: 2026-11-05T02:01:10Z, in 63 seconds.\n"
    )
    assert slot_wait(page) == 23.0
    assert slot_wait("Connected as: 1\nRate limit: 0\n") is None


def test_429_waits_as_long_as_the_status_page_says(home, fake):
    answers = iter([(429, {}, b""), reply([])])
    fake.routes["/api/interpreter"] = lambda r: next(answers, reply([]))
    fake.routes["/api/status"] = lambda r: (
        200, {"Content-Type": "text/plain"}, b"Rate limit: 2\nSlot available after: 2026-11-05T02:00:30Z, in 7 seconds.\n"
    )
    client, store = make_client(home, [fake.url + "/api/interpreter"])
    waits = []
    client._wait = lambda seconds, cancel: waits.append(seconds)
    client.fetch_cell(1, 1, threading.Event(), lambda p: None)
    assert waits == [7.0]
    assert fake.count("/api/status") == 1
    store.close()


def test_busy_server_is_asked_after_the_others(home, fake):
    fake.routes["/a"] = lambda r: (504, {}, b"busy")
    fake.routes["/b"] = lambda r: reply([])
    client, store = make_client(home, [fake.url + "/a", fake.url + "/b"], busy_seconds=30)
    client.fetch_cell(1, 1, threading.Event(), lambda p: None)
    paths = [r.path for r in fake.requests]
    assert paths == ["/a", "/b", "/b", "/b", "/b"], "after its busy answer, /a waits behind /b"
    assert client._ordered_endpoints() == [fake.url + "/b", fake.url + "/a"]
    store.close()


def test_activity_and_received_bytes_are_reported(home, fake):
    fake.routes["/a"] = lambda r: reply([{"type": "way", "id": 1}])
    client, store = make_client(home, [fake.url + "/a"])
    activity, received = [], []
    client.fetch_cell(
        210, 97, threading.Event(), lambda p: None,
        activity=lambda text, since=None, until=None: activity.append((text, since, until)),
        received=received.append,
    )
    assert len(activity) == 4 and all("127.0.0.1" in text and since for text, since, _ in activity)
    assert sum(received) == sum(len(json.dumps({"elements": [{"type": "way", "id": 1}]})) for _ in range(4))
    store.close()


def test_rate_limit_is_read_from_the_status_page():
    from offline_railway_map.overpass import rate_limit

    assert rate_limit("Connected as: 1\nCurrent time: x\nRate limit: 2\n2 slots available now.\n") == 2
    assert rate_limit("Rate limit: 0\n") == 0
    assert rate_limit("nothing here") is None


def test_slots_come_from_the_server_status(home, fake):
    fake.routes["/one/api/status"] = lambda r: (200, {}, b"Rate limit: 1\n1 slots available now.\n")
    fake.routes["/unlimited/api/status"] = lambda r: (200, {}, b"Rate limit: 0\n")
    fake.routes["/many/api/status"] = lambda r: (200, {}, b"Rate limit: 6\n")
    session = requests.Session()
    store = StateStore(home)
    client = OverpassClient(session, lambda: [], store, min_interval=0)
    assert client._slot_count(fake.url + "/one/api/interpreter") == 1
    assert client._slot_count(fake.url + "/unlimited/api/interpreter") == 2, "polite cap when unlimited"
    assert client._slot_count(fake.url + "/many/api/interpreter") == 2, "never more than 2 per server"
    assert client._slot_count(fake.url + "/silent/api/interpreter") == 1, "unknown: one at a time"
    assert client._slot_count(fake.url + "/no-status-page") == 1
    assert client._slot_count("https://overpass-api.de/api/interpreter") == 1, "its policy: no parallel queries"
    store.close()


def test_cells_in_parallel_use_free_servers_within_their_slots(home, fake):
    """Three cells at once, two servers with one slot each: never more than one
    query per server, both servers busy at the same time."""
    lock = threading.Lock()
    running = {"/a": 0, "/b": 0}
    peak = {"/a": 0, "/b": 0, "total": 0}

    def route(rec):
        with lock:
            running[rec.path] += 1
            peak[rec.path] = max(peak[rec.path], running[rec.path])
            peak["total"] = max(peak["total"], sum(running.values()))
        time.sleep(0.15)
        with lock:
            running[rec.path] -= 1
        return reply([{"type": "way", "id": 1}])

    fake.routes["/a"] = route
    fake.routes["/b"] = route
    client, store = make_client(home, [fake.url + "/a", fake.url + "/b"])
    results = {}

    def fetch(x):
        results[x] = client.fetch_cell(x, 97, threading.Event(), lambda p: None)

    threads = [threading.Thread(target=fetch, args=(x,)) for x in (210, 211, 212)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(results) == [210, 211, 212]
    assert peak["/a"] == 1 and peak["/b"] == 1, peak
    assert peak["total"] == 2, "both servers worked at the same time"
    assert len(fake.requests) == 3 * 4
    store.close()


def test_requests_to_one_server_start_a_second_apart(home, fake):
    fake.routes["/a"] = lambda r: reply([])
    client, store = make_client(home, [fake.url + "/a"])
    client.min_interval = 0.2
    started = time.monotonic()
    client.fetch_cell(1, 1, threading.Event(), lambda p: None)  # four parts
    assert time.monotonic() - started >= 3 * 0.2 - 0.01
    store.close()
