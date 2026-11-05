"""Railway data for one cell from the Overpass API (SPEC §9.2, appendix D).

* a cell is fetched as its four z9 quarters (faster answers, accepted more
  readily by busy servers, and progress the user can see);
* one request at a time, at least 1 s apart;
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

Activity = Callable[..., None]  # activity(text, since=None, until=None)


def _no_activity(text: str, since: float | None = None, until: float | None = None) -> None:
    pass


def host_of(endpoint: str) -> str:
    return urlsplit(endpoint).hostname or endpoint


def status_url(endpoint: str) -> str | None:
    """``.../api/interpreter`` -> ``.../api/status`` (Overpass rate-limit information)."""
    return endpoint[: -len("interpreter")] + "status" if endpoint.endswith("/interpreter") else None


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
    ) -> None:
        self.session = session
        self.endpoints = endpoints
        self.store = store
        self.min_interval = min_interval
        self.default_retry_after = default_retry_after
        self.busy_backoff = busy_backoff
        self.busy_response_seconds = busy_response_seconds
        self._last_request = 0.0
        self._lock = threading.Lock()
        self._down_until: dict[str, float] = {}
        self._trouble_until: dict[str, float] = {}
        # For the cell being downloaded, kept in memory while the program runs:
        # parts already downloaded and the part size known to be too large, so a
        # retry (the cell goes back to the queue) continues where it stopped.
        self._progress: dict[tuple[int, int], dict] = {}

    def forget_cell(self, x: int, y: int) -> None:
        self._progress.pop((x, y), None)

    def forget_all(self) -> None:
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
        if (x, y) not in self._progress:
            self._progress.clear()  # only one cell's parts are kept (memory)
        state = self._progress.setdefault((x, y), {"heavy": -1, "done": {}})
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
        self._progress.pop((x, y), None)
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
            for endpoint in self._ordered_endpoints():
                if not self._within_budget(endpoint):
                    continue
                tried += 1
                rate_limited = 0
                host = host_of(endpoint)
                while True:
                    self._pace(cancel)
                    started = time.monotonic()
                    activity(f"等待 {host} 返回数据", since=time.time())
                    try:
                        elements = self._post(endpoint, query, cancel, read_timeout, received)
                        self._trouble_until.pop(endpoint, None)
                        self._down_until.pop(endpoint, None)
                        return elements
                    except requests.ConnectionError as exc:
                        connection_failures += 1
                        self._down_until[endpoint] = time.monotonic() + DOWN_FOR
                        last_problem = f"{endpoint}: 无法连接"
                        problems.append(f"{host} 无法连接")
                        log.warning("Overpass %s unreachable: %s", endpoint, exc)
                        break
                    except requests.Timeout:
                        heavy_failures += 1
                        self._trouble_until[endpoint] = time.monotonic() + TROUBLE_FOR
                        last_problem = f"{endpoint}: 超时"
                        problems.append(f"{host} 超时")
                        log.info("Overpass %s timed out", endpoint)
                        break
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
                        if exc.status in (429, 502, 503, 504):
                            if time.monotonic() - started < self.busy_response_seconds:
                                busy += 1
                            else:
                                heavy_failures += 1
                            self._trouble_until[endpoint] = time.monotonic() + TROUBLE_FOR
                        last_problem = f"{endpoint}: {exc}"
                        problems.append(f"{host} {exc}")
                        log.info("Overpass %s answered %s", endpoint, exc)
                        break
                    except _NeedsSplit as exc:
                        heavy_failures += 1
                        last_problem = f"{endpoint}: {exc}"
                        problems.append(f"{host} 无法完成查询")
                        break
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

    def _pace(self, cancel: threading.Event) -> None:
        if cancel.is_set():
            raise Cancelled()
        with self._lock:
            wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            self._wait(wait, cancel)
        with self._lock:
            self._last_request = time.monotonic()

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
