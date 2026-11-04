"""Railway data for one cell from the Overpass API (SPEC §9.2, appendix D).

* one request at a time, at least 1 s apart;
* 429 -> wait for Retry-After (60 s if absent), up to 3 times per server,
  then the next server;
* a 502/503/504 that comes back quickly means the server is busy: try the
  next server; when every server is busy, wait (30 s, 60 s) and try again;
  if they are still busy the area is split (servers often accept smaller
  queries); at the smallest size keep waiting (up to 5 min) and finally
  raise ``ServersBusy`` (the cell goes back to the queue, not failed);
* a timeout, a slow 504 or "out of memory" means the area is too heavy: one
  more server, then the area is split into 4 (z9), 16 (z10) and, if needed,
  64 (z11) parts;
* connection failures on every server -> ``Offline`` (not a cell failure);
  a server that could not be reached is tried last for the next 10 minutes;
* per-server daily budget (requests and bytes) -> ``BudgetExhausted``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import requests

from .grid import CELL_ZOOM, tile_bounds
from .store import StateStore

log = logging.getLogger(__name__)

QUERY = (
    "[out:json][timeout:{timeout}][maxsize:1073741824];\n"
    'way["railway"~"^(rail|narrow_gauge|subway|light_rail|tram|construction|proposed|disused)$"]({s},{w},{n},{e});\n'
    "out tags geom;\n"
    'nwr["railway"~"^(station|halt)$"]({s},{w},{n},{e});\n'
    "out tags center;"
)
CONNECT_TIMEOUT = 20
# Server-side timeout per part size: smaller parts should be quick, and a
# shorter limit lets a stuck server be left sooner.  Client waits 30 s longer.
SERVER_TIMEOUT = {8: 300, 9: 240, 10: 180, 11: 120}
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

    # -- public ------------------------------------------------------------------------

    def fetch_cell(self, x: int, y: int, cancel: threading.Event, progress: Callable[[float], None]) -> list[dict]:
        """All railway ways and stations touching the cell, deduplicated."""
        found: dict[tuple[str, int], dict] = {}
        pending = [(CELL_ZOOM, x, y, 1.0)]
        done = 0.0
        heavy_zoom = -1  # deepest part size of this cell that the servers refused
        while pending:
            z, tx, ty, weight = pending.pop(0)
            children = [(z + 1, 2 * tx + dx, 2 * ty + dy, weight / 4) for dy in (0, 1) for dx in (0, 1)]
            if z <= heavy_zoom and z < MAX_SPLIT_ZOOM:
                # a part of this size was already refused: go straight to smaller parts
                pending[0:0] = children
                continue
            try:
                elements = self._query_area(
                    tile_bounds(z, tx, ty), cancel, can_split=z < MAX_SPLIT_ZOOM, timeout=SERVER_TIMEOUT.get(z, 120)
                )
            except _NeedsSplit as exc:
                if z >= MAX_SPLIT_ZOOM:
                    raise QueryFailed(f"Overpass 服务器拆分查询后仍无法提供该区域的数据（{exc}）") from None
                log.info("splitting %d/%d/%d after: %s", z, tx, ty, exc)
                heavy_zoom = max(heavy_zoom, z)
                pending[0:0] = children
                continue
            for element in elements:
                found[(element["type"], element["id"])] = element
            done += weight
            progress(min(1.0, done))
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
        """Configured order, but servers that were just unreachable go last."""
        now = time.monotonic()
        endpoints = list(self.endpoints())
        return [e for e in endpoints if self._down_until.get(e, 0) <= now] + [
            e for e in endpoints if self._down_until.get(e, 0) > now
        ]

    def _query_area(self, bounds, cancel: threading.Event, can_split: bool = True, timeout: int = 300) -> list[dict]:
        query = build_query(bounds, timeout)
        read_timeout = timeout + READ_MARGIN
        for attempt in range(len(self.busy_backoff) + 1):
            tried = connection_failures = heavy_failures = busy = 0
            last_problem = "no server available"
            for endpoint in self._ordered_endpoints():
                if not self._within_budget(endpoint):
                    continue
                tried += 1
                rate_limited = 0
                while True:
                    self._pace(cancel)
                    started = time.monotonic()
                    try:
                        return self._post(endpoint, query, cancel, read_timeout)
                    except requests.ConnectionError as exc:
                        connection_failures += 1
                        self._down_until[endpoint] = time.monotonic() + DOWN_FOR
                        last_problem = f"{endpoint}: 无法连接"
                        log.warning("Overpass %s unreachable: %s", endpoint, exc)
                        break
                    except requests.Timeout:
                        heavy_failures += 1
                        last_problem = f"{endpoint}: 超时"
                        break
                    except _HttpError as exc:
                        if exc.status == 429 and rate_limited < MAX_429_RETRIES:
                            rate_limited += 1
                            self._wait(exc.retry_after or self.default_retry_after, cancel)
                            continue
                        if exc.status == 400:
                            raise QueryFailed(f"{endpoint} 拒绝了查询（rejected the query）") from None
                        if exc.status in (429, 502, 503, 504):
                            if time.monotonic() - started < self.busy_response_seconds:
                                busy += 1
                            else:
                                heavy_failures += 1
                        last_problem = f"{endpoint}: {exc}"
                        log.info("Overpass %s answered %s", endpoint, exc)
                        break
                    except _NeedsSplit as exc:
                        heavy_failures += 1
                        last_problem = f"{endpoint}: {exc}"
                        break
                if heavy_failures >= HEAVY_FAILURES_BEFORE_SPLIT:
                    raise _NeedsSplit(last_problem)
            if tried == 0:
                raise BudgetExhausted("today's Overpass usage limit is reached on every server")
            if connection_failures == tried:
                raise Offline("no Overpass server can be reached")
            if heavy_failures:
                raise _NeedsSplit(last_problem)
            # every reachable server was busy: split if possible, otherwise wait and retry
            if can_split and attempt + 1 >= BUSY_ROUNDS_BEFORE_SPLIT:
                raise _NeedsSplit(f"servers keep refusing this area ({last_problem})")
            if attempt < len(self.busy_backoff):
                log.info("all Overpass servers busy, waiting %.0f s", self.busy_backoff[attempt])
                self._wait(self.busy_backoff[attempt], cancel)
        raise ServersBusy(f"Overpass servers are busy ({last_problem})")

    def _post(self, endpoint: str, query: str, cancel: threading.Event, read_timeout: float = 330) -> list[dict]:
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
