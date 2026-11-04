"""Online raster tiles for uncached areas (SPEC §7.2, §7.8, §10.3, §12.2, §12.3).

* tiles are requested only when the page asks for them (current view);
* the disk cache follows the servers' caching headers (7 days when absent);
* expired tiles are revalidated with If-None-Match / If-Modified-Since;
* ORM answers 429 -> stop asking ORM for 10 minutes;
* repeated failures -> exponential back-off from 1 to 15 minutes;
* a server refusal (OSM "x-blocked", 403) pauses that service for 30 minutes
  and is never cached as a tile;
* parts covered by cached cells are made transparent, and tiles fully inside
  cached cells are never requested.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Callable

import requests

from .coverage import CellCoverage
from .masking import TRANSPARENT_PNG, apply_mask
from .net import Http, NetStatus
from .paths import Home

log = logging.getLogger(__name__)

DEFAULT_TTL = 7 * 24 * 3600
FAILURES_BEFORE_BACKOFF = 5
BACKOFF_START, BACKOFF_MAX = 60.0, 900.0
BLOCK_PAUSE = 1800.0


@dataclass(frozen=True)
class OnlineSource:
    name: str
    label: str
    url_template: str
    timeout: float
    min_zoom: int
    max_zoom: int
    pause_on_429: float = 600.0


SOURCES = (
    OnlineSource("osm", "OSM 底图", "https://tile.openstreetmap.org/{z}/{x}/{y}.png", 10.0, 0, 19),
    OnlineSource("orm", "ORM 瓦片", "https://tiles.openrailwaymap.org/maxspeed/{z}/{x}/{y}.png", 5.0, 2, 19),
)


@dataclass
class CachedTile:
    data: bytes
    etag: str | None
    last_modified: str | None
    expires: float


def expiry_from_headers(headers, now: float) -> tuple[float, bool]:
    """Return (expires_at, storable) following Cache-Control / Expires."""
    directives: dict[str, str | None] = {}
    for part in headers.get("Cache-Control", "").split(","):
        name, _, value = part.strip().partition("=")
        if name:
            directives[name.lower()] = value.strip('"') or None
    if "no-store" in directives:
        return now, False
    if "no-cache" in directives:
        return now, True
    if directives.get("max-age"):
        try:
            return now + max(0, int(directives["max-age"])), True
        except ValueError:
            pass
    expires = headers.get("Expires")
    if expires:
        try:
            return parsedate_to_datetime(expires).timestamp(), True
        except (TypeError, ValueError):
            return now, True
    return now + DEFAULT_TTL, True


MEMORY_ONLY_LIMIT = 256 * 1024 * 1024


class OnlineTileCache:
    """``data/cache/online_tiles.sqlite``; created when the first tile is stored.

    After 「清空缓存」 the cache runs in memory only (``memory_only``), so that
    reloading the map does not recreate the data folder; it goes back to disk
    as soon as something else creates the data folder (SPEC §6.6).
    """

    def __init__(self, home: Home, limit_bytes: Callable[[], int]) -> None:
        self.home = home
        self._limit_bytes = limit_bytes
        self._conn: sqlite3.Connection | None = None
        # Re-entrant: creating the data folder (inside put) runs _disk_allowed.
        self._lock = threading.RLock()
        self._total = 0
        self.memory_only = False
        self._memory: OrderedDict[tuple, CachedTile] = OrderedDict()
        self._memory_bytes = 0
        home.on_data_created(self._disk_allowed)

    def _disk_allowed(self) -> None:
        with self._lock:
            self.memory_only = False
            self._memory.clear()
            self._memory_bytes = 0

    @property
    def path(self):
        return self.home.cache_dir / "online_tiles.sqlite"

    def _connection(self, create: bool) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        if not create and not self.path.exists():
            return None
        self.home.ensure_dir(self.home.cache_dir)
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tiles (source TEXT, z INTEGER, x INTEGER, y INTEGER, data BLOB, "
            "etag TEXT, last_modified TEXT, expires REAL, used REAL, size INTEGER, "
            "PRIMARY KEY (source, z, x, y))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS tiles_used ON tiles(used)")
        self._total = conn.execute("SELECT COALESCE(SUM(size), 0) FROM tiles").fetchone()[0]
        self._conn = conn
        return conn

    def get(self, source: str, z: int, x: int, y: int) -> CachedTile | None:
        with self._lock:
            if self.memory_only:
                tile = self._memory.get((source, z, x, y))
                if tile is not None:
                    self._memory.move_to_end((source, z, x, y))
                return tile
            conn = self._connection(create=False)
            if conn is None:
                return None
            row = conn.execute(
                "SELECT data, etag, last_modified, expires, used FROM tiles WHERE source=? AND z=? AND x=? AND y=?",
                (source, z, x, y),
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            if now - (row[4] or 0) > 3600:  # keep LRU order without a write on every hit
                conn.execute(
                    "UPDATE tiles SET used=? WHERE source=? AND z=? AND x=? AND y=?", (now, source, z, x, y)
                )
            return CachedTile(row[0], row[1], row[2], row[3])

    def put(self, source: str, z: int, x: int, y: int, tile: CachedTile) -> None:
        with self._lock:
            if self.memory_only:
                key = (source, z, x, y)
                old = self._memory.pop(key, None)
                self._memory_bytes += len(tile.data) - (len(old.data) if old else 0)
                self._memory[key] = tile
                while self._memory_bytes > MEMORY_ONLY_LIMIT and self._memory:
                    _, dropped = self._memory.popitem(last=False)
                    self._memory_bytes -= len(dropped.data)
                return
            conn = self._connection(create=True)
            old = conn.execute(
                "SELECT size FROM tiles WHERE source=? AND z=? AND x=? AND y=?", (source, z, x, y)
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO tiles VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source, z, x, y, tile.data, tile.etag, tile.last_modified, tile.expires, time.time(), len(tile.data)),
            )
            self._total += len(tile.data) - (old[0] if old else 0)
            self._evict(conn)

    def refresh(self, source: str, z: int, x: int, y: int, expires: float) -> None:
        with self._lock:
            if self.memory_only:
                tile = self._memory.get((source, z, x, y))
                if tile is not None:
                    tile.expires = expires
                return
            conn = self._connection(create=False)
            if conn is not None:
                conn.execute(
                    "UPDATE tiles SET expires=?, used=? WHERE source=? AND z=? AND x=? AND y=?",
                    (expires, time.time(), source, z, x, y),
                )

    def _evict(self, conn: sqlite3.Connection) -> None:
        limit = self._limit_bytes()
        if self._total <= limit:
            return
        target = int(limit * 0.9)
        while self._total > target:
            rows = conn.execute("SELECT source, z, x, y, size FROM tiles ORDER BY used LIMIT 200").fetchall()
            if not rows:
                break
            conn.executemany(
                "DELETE FROM tiles WHERE source=? AND z=? AND x=? AND y=?", [r[:4] for r in rows]
            )
            self._total -= sum(r[4] for r in rows)

    @property
    def total_bytes(self) -> int:
        return self._total

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._total = 0


class _Breaker:
    def __init__(self) -> None:
        self.paused_until = 0.0
        self.failures = 0
        self.lock = threading.Lock()


class _Flight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: bytes | None = None


class OnlineTiles:
    def __init__(
        self,
        home: Home,
        http: Http,
        status: NetStatus,
        coverage: CellCoverage,
        cache_limit_mb: Callable[[], int],
        sources: tuple[OnlineSource, ...] = SOURCES,
    ) -> None:
        self.http = http
        self.status = status
        self.coverage = coverage
        self.sources = {s.name: s for s in sources}
        self.cache = OnlineTileCache(home, lambda: cache_limit_mb() * 1024 * 1024)
        self._breakers = {name: _Breaker() for name in self.sources}
        self._flights: dict[tuple, _Flight] = {}
        self._flights_lock = threading.Lock()

    def tile(self, name: str, z: int, x: int, y: int) -> bytes:
        source = self.sources[name]
        if not (source.min_zoom <= z <= source.max_zoom and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
            return TRANSPARENT_PNG
        mask = self.coverage.tile_mask(z, x, y)
        if mask is not None and mask.full:
            return TRANSPARENT_PNG
        data = self._fetch(source, z, x, y)
        if data is None:
            return TRANSPARENT_PNG
        if mask is not None:
            return apply_mask(data, mask.rects)
        return data

    # -- fetching ----------------------------------------------------------------------

    def _fetch(self, source: OnlineSource, z: int, x: int, y: int) -> bytes | None:
        cached = self.cache.get(source.name, z, x, y)
        now = time.time()
        if cached is not None and cached.expires > now:
            return cached.data
        breaker = self._breakers[source.name]
        if now < breaker.paused_until:
            return cached.data if cached else None
        key = (source.name, z, x, y)
        with self._flights_lock:
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = self._flights[key] = _Flight()
        if not leader:
            flight.done.wait(source.timeout + 5)
            return flight.result if flight.result is not None else (cached.data if cached else None)
        try:
            flight.result = self._download(source, z, x, y, cached)
            return flight.result
        finally:
            flight.done.set()
            with self._flights_lock:
                self._flights.pop(key, None)

    def _download(self, source: OnlineSource, z: int, x: int, y: int, cached: CachedTile | None) -> bytes | None:
        stale = cached.data if cached else None
        url = source.url_template.format(z=z, x=x, y=y)
        headers = {}
        if cached is not None and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached is not None and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        try:
            response = self.http.get(url, timeout=source.timeout, headers=headers)
        except requests.ConnectionError:
            self.status.network_down()
            self._failure(source)
            return stale
        except requests.RequestException:
            self._failure(source)  # slow or broken server; the network itself may be fine
            return stale
        self.status.network_ok()
        now = time.time()
        if "x-blocked" in response.headers or response.status_code in (403, 418, 451):
            reason = response.headers.get("x-blocked") or f"HTTP {response.status_code}"
            if time.time() >= self._breakers[source.name].paused_until:  # log once per pause
                log.warning("%s refused the request: %s", source.name, reason)
            self._pause(source, BLOCK_PAUSE, "blocked", f"{source.label}拒绝访问：{reason}")
            return stale
        if response.status_code == 429:
            delay = source.pause_on_429
            try:
                delay = max(delay, float(response.headers.get("Retry-After", 0)))
            except ValueError:
                pass
            self._pause(source, delay, "paused", f"{source.label}请求过多，已暂停访问")
            return stale
        if response.status_code == 304 and cached is not None:
            expires, _ = expiry_from_headers(response.headers, now)
            self.cache.refresh(source.name, z, x, y, expires)
            self._success(source)
            return cached.data
        if response.status_code == 404:
            self._success(source)
            return None
        if response.status_code != 200 or not response.content:
            self._failure(source)
            return stale
        if not response.headers.get("Content-Type", "image/png").startswith("image/"):
            self._failure(source)
            return stale
        expires, storable = expiry_from_headers(response.headers, now)
        if storable:
            tile = CachedTile(
                response.content, response.headers.get("ETag"), response.headers.get("Last-Modified"), expires
            )
            try:
                self.cache.put(source.name, z, x, y, tile)
            except (OSError, sqlite3.Error):
                log.exception("could not store tile in the cache")
        self._success(source)
        return response.content

    # -- back-off -------------------------------------------------------------------------

    def _success(self, source: OnlineSource) -> None:
        breaker = self._breakers[source.name]
        with breaker.lock:
            had_trouble = breaker.failures or breaker.paused_until
            breaker.failures = 0
            breaker.paused_until = 0.0
        if had_trouble:
            self.status.service(source.name, "ok")

    def _failure(self, source: OnlineSource) -> None:
        breaker = self._breakers[source.name]
        with breaker.lock:
            breaker.failures += 1
            if breaker.failures < FAILURES_BEFORE_BACKOFF:
                return
            steps = breaker.failures - FAILURES_BEFORE_BACKOFF
            delay = min(BACKOFF_START * (2**steps), BACKOFF_MAX)
            breaker.paused_until = time.time() + delay
            until = breaker.paused_until
        self.status.service(source.name, "paused", f"{source.label}连续请求失败，稍后重试", until)

    def _pause(self, source: OnlineSource, seconds: float, state: str, message: str) -> None:
        breaker = self._breakers[source.name]
        with breaker.lock:
            breaker.paused_until = time.time() + seconds
            until = breaker.paused_until
        self.status.service(source.name, state, message, until)

    def close(self) -> None:
        self.cache.close()
