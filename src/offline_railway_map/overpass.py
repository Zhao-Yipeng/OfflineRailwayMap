"""Railway data for one cell from the Overpass API (SPEC §9.2, appendix D).

* a cell is fetched as its four z9 quarters (faster answers, accepted more
  readily by busy servers, and progress the user can see);
* several cells can be downloaded at once (downloader.py); each server gets at
  most as many queries at a time as its /api/status allows ("Rate limit: N"),
  never more than 2, and 1 if it does not say; requests to one server start at
  least 1 s apart; a query goes to the first server (in the order below) that
  has a free slot;
* 429 -> wait as long as the server's /api/status says (Retry-After, or 60 s
  if neither is known), up to 3 times per server, then the next server;
* a 502/503/504 that comes back quickly means the server is busy: try the
  next server; when every server is busy, wait (30 s, 60 s) and try again;
  if they are still busy the area is split (servers often accept smaller
  queries); at the smallest size keep waiting (up to 5 min) and finally
  raise ``ServersBusy`` (the cell goes back to the queue, not failed);
* a timeout, a slow 504 or "out of memory" means the area is too heavy: one
  more server, then the area is split into 4 (z9), 16 (z10) and, if needed,
  64 (z11) parts;
* connection failures on every server -> ``Offline`` (not a cell failure);
  a server that could not be reached is tried last for the next 10 minutes,
  one that just answered "busy" or timed out comes after the others for 5;
* per-server daily budget (requests and bytes) -> ``BudgetExhausted``;
* what is happening (which server, waiting since / retrying at, bytes
  received) is reported through ``activity`` and ``received`` callbacks.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import requests

from .grid import CELL_ZOOM, tile_bounds
from .store import StateStore

log = logging.getLogger(__name__)

# No [maxsize:...]: the server's default (512 MiB) is plenty for railways, and a
# bigger reservation makes a busy server refuse the query.
QUERY = (
    "[out:json][timeout:{timeout}];\n"
    'way["railway"~"^(rail|narrow_gauge|subway|light_rail|tram|construction|proposed|disused)$"]({s},{w},{n},{e});\n'
    "out tags geom;\n"
    'nwr["railway"~"^(station|halt)$"]({s},{w},{n},{e});\n'
    "out tags center;"
)
CONNECT_TIMEOUT = 20
# Server-side timeout per part size: smaller parts should be quick, and a
# shorter limit lets a stuck server be left sooner and is accepted more
# readily when the server is busy.  The client waits 30 s longer.
SERVER_TIMEOUT = {9: 180, 10: 120, 11: 90}
FIRST_ZOOM = CELL_ZOOM + 1  # a cell is fetched as four parts
READ_MARGIN = 30
MAX_RESPONSE = 200 * 1024 * 1024
DAILY_REQUESTS = 9000
DAILY_BYTES = 900 * 1024 * 1024
MAX_SPLIT_ZOOM = 11  # a cell may be split into at most 64 parts
MIN_INTERVAL = 1.0
MAX_429_RETRIES = 3
HEAVY_FAILURES_BEFORE_SPLIT = 2
BUSY_RESPONSE_SECONDS = 30.0  # a 5xx faster than this means "busy", not "too heavy"
BUSY_BACKOFF = (30.0, 60.0, 120.0, 240.0, 300.0)
# A server that keeps refusing a large area at once often accepts smaller ones,
# so after this many busy rounds the area is split if it still can be.
BUSY_ROUNDS_BEFORE_SPLIT = 2
DOWN_FOR = 600.0
TROUBLE_FOR = 300.0  # a server that was busy or timed out goes after the others
MAX_SLOT_WAIT = 300.0
MAX_SLOTS_PER_SERVER = 2  # queries at once on one server, even if it allows more
# Servers whose usage policy allows fewer queries at once than their status
# page says: overpass-api.de asks for "no parallel running of multiple scripts".
SERVER_SLOT_CAPS = {"overpass-api.de": 1}
REMEMBERED_CELLS = 8  # partly downloaded cells kept in memory (see fetch_cell)

Activity = Callable[..., None]  # activity(text, since=None, until=None)


def _no_activity(text: str, since: float | None = None, until: float | None = None) -> None:
    pass


def host_of(endpoint: str) -> str:
    return urlsplit(endpoint).hostname or endpoint


def status_url(endpoint: str) -> str | None:
    """``.../api/interpreter`` -> ``.../api/status`` (Overpass rate-limit information)."""
    return endpoint[: -len("interpreter")] + "status" if endpoint.endswith("/interpreter") else None


def rate_limit(status_text: str) -> int | None:
    """Query slots per client from an Overpass ``/api/status`` page (0 = no limit)."""
    match = re.search(r"^\s*Rate limit:\s*(\d+)", status_text, re.M)
    return int(match.group(1)) if match else None


def slot_wait(status_text: str) -> float | None:
    """Seconds until a query slot is free, from an Overpass ``/api/status`` page."""
    if re.search(r"^\s*[1-9]\d* slots? available now", status_text, re.M):
        return 0.0
    waits = [float(m) for m in re.findall(r"Slot available after: \S+, in (-?\d+) seconds?", status_text)]
    return max(0.0, min(waits)) if waits else None


class Cancelled(Exception):
    """The cell was removed or the program is closing."""


class Offline(Exception):
    """No Overpass server could be reached."""


class BudgetExhausted(Exception):
    """Every server reached today's usage limit."""


class QueryFailed(Exception):
    """The area could not be downloaded, even split into small parts."""


class ServersBusy(Exception):
    """Every reachable server kept answering "busy"; try again later."""


class _NeedsSplit(Exception):
    pass


class _HttpError(Exception):
    def __init__(self, status: int, retry_after: float | None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def build_query(bounds: tuple[float, float, float, float], timeout: int = 300) -> str:
    west, south, east, north = bounds
    return QUERY.format(timeout=timeout, s=f"{south:.7f}", w=f"{west:.7f}", n=f"{north:.7f}", e=f"{east:.7f}")


class OverpassClient:
    def __init__(
        self,
        session: requests.Session,
        endpoints: Callable[[], list[str]],
        store: StateStore,
        *,
        min_interval: float = MIN_INTERVAL,
        default_retry_after: float = 60.0,
        busy_backoff: tuple[float, ...] = BUSY_BACKOFF,
        busy_response_seconds: float = BUSY_RESPONSE_SECONDS,
        server_slots: Callable[[str], int] | None = None,
    ) -> None:
        self.session = session
        self.endpoints = endpoints
        self.store = store
        self.min_interval = min_interval
        self.default_retry_after = default_retry_after
        self.busy_backoff = busy_backoff
        self.busy_response_seconds = busy_response_seconds
        self._server_slots = server_slots  # tests fix the slots instead of asking the server
        self._lock = threading.Lock()
        self._last_request: dict[str, float] = {}
        self._slots: dict[str, threading.BoundedSemaphore] = {}
        self._slots_lock = threading.Lock()
        self._asking: dict[str, threading.Lock] = {}
        self._down_until: dict[str, float] = {}
        self._trouble_until: dict[str, float] = {}
        # For cells being downloaded, kept in memory while the program runs:
        # parts already downloaded and the part size known to be too large, so a
        # retry (the cell goes back to the queue) continues where it stopped.
        self._progress: dict[tuple[int, int], dict] = {}

    def forget_cell(self, x: int, y: int) -> None:
        with self._lock:
            self._progress.pop((x, y), None)

    def forget_all(self) -> None:
        with self._lock:
            self._progress.clear()

    # -- public ------------------------------------------------------------------------

    def fetch_cell(
        self,
        x: int,
        y: int,
        cancel: threading.Event,
        progress: Callable[[float], None],
        activity: Activity = _no_activity,
        received: Callable[[int], None] = lambda n: None,
    ) -> list[dict]:
        """All railway ways and stations touching the cell, deduplicated."""
        with self._lock:
            state = self._progress.pop((x, y), None) or {"heavy": -1, "done": {}}
            self._progress[(x, y)] = state  # most recently used last
            while len(self._progress) > REMEMBERED_CELLS:  # bound the memory used
                self._progress.pop(next(iter(self._progress)))
        found: dict[tuple[str, int], dict] = {}
        pending = [(FIRST_ZOOM, 2 * x + dx, 2 * y + dy, 0.25) for dy in (0, 1) for dx in (0, 1)]
        done = 0.0
        while pending:
            z, tx, ty, weight = pending.pop(0)
            children = [(z + 1, 2 * tx + dx, 2 * ty + dy, weight / 4) for dy in (0, 1) for dx in (0, 1)]
            elements = state["done"].get((z, tx, ty))
            if elements is None:
                if z <= state["heavy"] and z < MAX_SPLIT_ZOOM:
                    # a part of this size was already refused: go straight to smaller parts
                    pending[0:0] = children
                    continue
                try:
                    elements = self._query_area(
                        tile_bounds(z, tx, ty), cancel, can_split=z < MAX_SPLIT_ZOOM,
                        timeout=SERVER_TIMEOUT.get(z, 90), activity=activity, received=received,
                    )
                except _NeedsSplit as exc:
                    log.info("splitting %d/%d/%d after: %s", z, tx, ty, exc)
                    activity("这一块数据太多或服务器太忙，拆成更小的块下载")
                    state["heavy"] = max(state["heavy"], z)
                    pending[0:0] = children
                    continue
                state["done"][(z, tx, ty)] = elements
            for element in elements:
                found[(element["type"], element["id"])] = element
            done += weight
            progress(min(1.0, done))
        self.forget_cell(x, y)
        return list(found.values())

    def lines_near(self, lon: float, lat: float, radius: float) -> list[dict]:
        """Tags of railway lines within ``radius`` metres of a point (click details)."""
        query = (
            "[out:json][timeout:25];"
            f'way(around:{radius:.0f},{lat:.7f},{lon:.7f})'
            '["railway"~"^(rail|narrow_gauge|subway|light_rail|tram|construction|proposed|disused)$"];'
            "out tags;"
        )
        last: Exception | None = None
        for endpoint in self._ordered_endpoints():
            if not self._within_budget(endpoint):
                continue
            try:
                response = self.session.post(endpoint, data={"data": query}, timeout=(10, 40))
                self._record(endpoint, len(response.content))
                if response.status_code == 200:
                    return [{"id": e["id"], "tags": e.get("tags", {})} for e in response.json().get("elements", [])]
                last = RuntimeError(f"HTTP {response.status_code}")
            except requests.RequestException as exc:
                last = exc
        raise RuntimeError(str(last) if last else "no Overpass server available")

    # -- one area ------------------------------------------------------------------------

    def _ordered_endpoints(self) -> list[str]:
        """Configured order, but servers that were just busy go after the
        others, and servers that were just unreachable go last."""
        now = time.monotonic()
        endpoints = list(self.endpoints())

        def rank(item: tuple[int, str]) -> tuple[int, int]:
            index, endpoint = item
            if self._down_until.get(endpoint, 0) > now:
                return (2, index)
            if self._trouble_until.get(endpoint, 0) > now:
                return (1, index)
            return (0, index)

        return [e for _i, e in sorted(enumerate(endpoints), key=rank)]

    def _query_area(
        self,
        bounds,
        cancel: threading.Event,
        can_split: bool = True,
        timeout: int = 180,
        activity: Activity = _no_activity,
        received: Callable[[int], None] = lambda n: None,
    ) -> list[dict]:
        query = build_query(bounds, timeout)
        read_timeout = timeout + READ_MARGIN
        for attempt in range(len(self.busy_backoff) + 1):
            tried = connection_failures = heavy_failures = busy = 0
            last_problem = "no server available"
            problems: list[str] = []
            # Each server once per round, the first one with a free slot first.
            remaining = [e for e in self._ordered_endpoints() if self._within_budget(e)]
            while remaining:
                endpoint = self._take_slot(remaining, cancel, activity)
                remaining.remove(endpoint)
                try:
                    outcome = self._ask(
                        endpoint, query, cancel, read_timeout, activity, received
                    )
                finally:
                    self._slots[endpoint].release()
                tried += 1
                kind, value = outcome
                if kind == "elements":
                    return value
                problems.append(value[1])
                last_problem = value[0]
                if kind == "offline":
                    connection_failures += 1
                elif kind == "heavy":
                    heavy_failures += 1
                elif kind == "busy":
                    busy += 1
                if can_split and heavy_failures >= HEAVY_FAILURES_BEFORE_SPLIT:
                    raise _NeedsSplit(last_problem)
            if tried == 0:
                raise BudgetExhausted("today's Overpass usage limit is reached on every server")
            if connection_failures == tried:
                raise Offline("no Overpass server can be reached")
            if can_split and (heavy_failures or attempt + 1 >= BUSY_ROUNDS_BEFORE_SPLIT):
                raise _NeedsSplit(last_problem)
            # Busy servers, or the smallest part still refused (that is overload,
            # not too much data): wait and try again.
            if attempt < len(self.busy_backoff):
                wait = self.busy_backoff[attempt]
                log.info("Overpass servers busy, waiting %.0f s", wait)
                activity(f"服务器都很忙（{'；'.join(problems)}），稍后重试", until=time.time() + wait)
                self._wait(wait, cancel)
        raise ServersBusy(f"Overpass 服务器繁忙（{last_problem}）")

    # -- one server --------------------------------------------------------------------

    def _slot_count(self, endpoint: str) -> int:
        """Queries this client may run at once on ``endpoint`` (its /api/status)."""
        if self._server_slots is not None:
            return max(1, self._server_slots(endpoint))
        cap = SERVER_SLOT_CAPS.get(host_of(endpoint), MAX_SLOTS_PER_SERVER)
        if cap == 1:
            return 1
        url = status_url(endpoint)
        if url is None:
            return 1
        try:
            response = self.session.get(url, timeout=(10, 15))
            limit = rate_limit(response.text) if response.status_code == 200 else None
        except requests.RequestException:
            limit = None
        if limit is None:
            return 1
        slots = cap if limit == 0 else min(limit, cap)
        log.info("Overpass %s: %d quer%s at a time", endpoint, slots, "y" if slots == 1 else "ies")
        return slots

    def _slot(self, endpoint: str) -> threading.BoundedSemaphore:
        with self._slots_lock:
            slot = self._slots.get(endpoint)
            if slot is not None:
                return slot
            asking = self._asking.setdefault(endpoint, threading.Lock())
        with asking:  # one status request per server; other servers are not held up
            with self._slots_lock:
                slot = self._slots.get(endpoint)
            if slot is None:
                slot = threading.BoundedSemaphore(self._slot_count(endpoint))
                with self._slots_lock:
                    slot = self._slots.setdefault(endpoint, slot)
            return slot

    def _take_slot(self, candidates: list[str], cancel: threading.Event, activity: Activity) -> str:
        """Wait for a free query slot on one of ``candidates`` (in order); return that server."""
        waited = False
        while True:
            for endpoint in candidates:
                if self._slot(endpoint).acquire(blocking=False):
                    return endpoint
            if not waited:
                activity("等待空闲的服务器（其他方格正在查询）", since=time.time())
                waited = True
            if cancel.wait(0.2):
                raise Cancelled()

    def _ask(
        self,
        endpoint: str,
        query: str,
        cancel: threading.Event,
        read_timeout: float,
        activity: Activity,
        received: Callable[[int], None],
    ) -> tuple[str, object]:
        """Ask one server, holding one of its slots.

        Returns ("elements", list) or (kind, (problem, short text for the UI))
        with kind "offline", "heavy" (too much data), "busy" or "other".
        """
        rate_limited = 0
        host = host_of(endpoint)
        while True:
            self._pace(endpoint, cancel)
            started = time.monotonic()
            activity(f"等待 {host} 返回数据", since=time.time())
            try:
                elements = self._post(endpoint, query, cancel, read_timeout, received)
                self._trouble_until.pop(endpoint, None)
                self._down_until.pop(endpoint, None)
                return "elements", elements
            except requests.ConnectionError as exc:
                self._down_until[endpoint] = time.monotonic() + DOWN_FOR
                log.warning("Overpass %s unreachable: %s", endpoint, exc)
                return "offline", (f"{endpoint}: 无法连接", f"{host} 无法连接")
            except requests.Timeout:
                self._trouble_until[endpoint] = time.monotonic() + TROUBLE_FOR
                log.info("Overpass %s timed out", endpoint)
                return "heavy", (f"{endpoint}: 超时", f"{host} 超时")
            except _HttpError as exc:
                if exc.status == 429 and rate_limited < MAX_429_RETRIES:
                    rate_limited += 1
                    wait = exc.retry_after
                    if wait is None:
                        wait = self._slot_wait(endpoint)
                    if wait is None:
                        wait = self.default_retry_after
                    wait = max(1.0, min(wait, MAX_SLOT_WAIT))
                    log.info("Overpass %s: rate limited, waiting %.0f s", endpoint, wait)
                    activity(f"{host} 限制了请求频率，按服务器要求等待", until=time.time() + wait)
                    self._wait(wait, cancel)
                    continue
                if exc.status == 400:
                    raise QueryFailed(f"{endpoint} 拒绝了查询（rejected the query）") from None
                log.info("Overpass %s answered %s", endpoint, exc)
                problem = (f"{endpoint}: {exc}", f"{host} {exc}")
                if exc.status in (429, 502, 503, 504):
                    self._trouble_until[endpoint] = time.monotonic() + TROUBLE_FOR
                    if time.monotonic() - started < self.busy_response_seconds:
                        return "busy", problem
                    return "heavy", problem
                return "other", problem
            except _NeedsSplit as exc:
                return "heavy", (f"{endpoint}: {exc}", f"{host} 无法完成查询")

    def _slot_wait(self, endpoint: str) -> float | None:
        """Ask the server when our next query slot is free (Overpass /api/status)."""
        url = status_url(endpoint)
        if url is None:
            return None
        try:
            response = self.session.get(url, timeout=(CONNECT_TIMEOUT, 30))
            if response.status_code != 200:
                return None
            return slot_wait(response.text)
        except requests.RequestException:
            return None

    def _post(
        self,
        endpoint: str,
        query: str,
        cancel: threading.Event,
        read_timeout: float = 210,
        received: Callable[[int], None] = lambda n: None,
    ) -> list[dict]:
        response = self.session.post(
            endpoint, data={"data": query}, timeout=(CONNECT_TIMEOUT, read_timeout), stream=True
        )
        with response:
            if response.status_code != 200:
                retry_after = None
                try:
                    retry_after = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    pass
                self._record(endpoint, 0)
                raise _HttpError(response.status_code, retry_after)
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=1 << 16):
                if cancel.is_set():
                    raise Cancelled()
                size += len(chunk)
                received(len(chunk))
                if size > MAX_RESPONSE:
                    self._record(endpoint, size)
                    raise _NeedsSplit("response larger than 200 MB")
                chunks.append(chunk)
        self._record(endpoint, size)
        try:
            data = json.loads(b"".join(chunks))
        except ValueError:
            raise _NeedsSplit("response is not valid JSON") from None
        remark = str(data.get("remark") or "")
        if "runtime error" in remark or "timed out" in remark or "out of memory" in remark:
            raise _NeedsSplit(remark)
        return data.get("elements", [])

    # -- pacing and budget -------------------------------------------------------------------

    def _pace(self, endpoint: str, cancel: threading.Event) -> None:
        """Requests to one server start at least ``min_interval`` apart."""
        while True:
            if cancel.is_set():
                raise Cancelled()
            with self._lock:
                wait = self._last_request.get(endpoint, 0.0) + self.min_interval - time.monotonic()
                if wait <= 0:
                    self._last_request[endpoint] = time.monotonic()
                    return
            self._wait(wait, cancel)

    @staticmethod
    def _wait(seconds: float, cancel: threading.Event) -> None:
        if cancel.wait(seconds):
            raise Cancelled()

    def _record(self, endpoint: str, size: int) -> None:
        try:
            self.store.add_usage(endpoint, 1, size)
        except Exception:
            log.exception("could not record Overpass usage")

    def _within_budget(self, endpoint: str) -> bool:
        requests_today, bytes_today = self.store.usage_today(endpoint)
        return requests_today < DAILY_REQUESTS and bytes_today < DAILY_BYTES
