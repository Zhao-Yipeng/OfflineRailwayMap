"""Online tiles for uncached areas (SPEC §7.2, §7.8, §10.3, §12.2, §12.3).

Two sources: the OpenFreeMap vector basemap and the OpenRailwayMap speed
layer (raster).

* tiles are requested only when the page asks for them (current view);
* the disk cache follows the servers' caching headers (7 days when absent;
  OpenFreeMap's ten-year headers are capped at 7 days so the map stays current);
* expired tiles are revalidated with If-None-Match / If-Modified-Since;
* 429 -> stop asking that server for 10 minutes;
* repeated failures -> exponential back-off from 1 to 15 minutes;
* a server refusal ("x-blocked", 403) pauses that service for 30 minutes
  and is never cached as a tile;
* tiles fully inside cached cells are never requested; for raster tiles the
  parts covered by cached cells are made transparent (vector tiles are hidden
  there by the page's mask layer instead);
* OpenFreeMap's tile URLs contain a weekly version: the current one is read
  from its TileJSON, which is re-read when a tile URL stops working.
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
from .paths import DataBlocked, Home

log = logging.getLogger(__name__)

DEFAULT_TTL = 7 * 24 * 3600
FAILURES_BEFORE_BACKOFF = 5
BACKOFF_START, BACKOFF_MAX = 60.0, 900.0
BLOCK_PAUSE = 1800.0


@dataclass(frozen=True)
class OnlineSource:
    name: str
    label: str
    url_template: str  # "{z}/{x}/{y}" template; empty when read from ``tilejson``
    timeout: float
    min_zoom: int
    max_zoom: int
    pause_on_429: float = 600.0
    kind: str = "raster"  # or "vector"
    tilejson: str | None = None
    max_ttl: float | None = None  # cap on the servers' cache lifetime
    parallel: int = 2  # concurrent requests to the server


SOURCES = (
    OnlineSource(
        "ofm", "在线底图（OpenFreeMap）", "", 10.0, 0, 14, kind="vector",
        tilejson="https://tiles.openfreemap.org/planet", max_ttl=7 * 24 * 3600, parallel=4,
    ),
    OnlineSource("orm", "ORM 瓦片", "https://tiles.openrailwaymap.org/maxspeed/{z}/{x}/{y}.png", 5.0, 2, 19),
)
VECTOR_TYPES = ("application/vnd.mapbox-vector-tile", "application/x-protobuf", "application/octet-stream")
TILEJSON_TTL = (600.0, 24 * 3600.0)  # re-read the TileJSON at most / at least this often
TILEJSON_RETRY = 30.0  # after a failed read, tiles wait this long before trying again


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
        if self.home.blocked:  # 「清空缓存」 in progress (SPEC §11.3)
            if create:
                raise DataBlocked("the data folder is being cleared")
            return None
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
        self._templates: dict[str, tuple[str, float]] = {}  # source -> (url template, valid until)
        self._templates_lock = threading.Lock()
        self._tilejson_fetch = threading.Lock()
        self._tilejson_failed: dict[str, float] = {}
        for source in sources:
            http.set_host_limit(source.tilejson or source.url_template, source.parallel)

    def tile(self, name: str, z: int, x: int, y: int) -> bytes:
        """The tile for the page: PNG for raster sources, MVT (b"" = empty) for vector ones."""
        source = self.sources[name]
        empty = b"" if source.kind == "vector" else TRANSPARENT_PNG
        if not (source.min_zoom <= z <= source.max_zoom and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
            return empty
        mask = self.coverage.tile_mask(z, x, y)
        if mask is not None and mask.full:
            return empty
        data = self._fetch(source, z, x, y)
        if data is None:
            return empty
        if mask is not None and source.kind == "raster":
            return apply_mask(data, mask.rects)
        return data

    # -- tile URLs -------------------------------------------------------------------------

    def _template(self, source: OnlineSource, refresh: bool = False) -> str | None:
        if source.tilejson is None:
            return source.url_template
        now = time.time()
        with self._templates_lock:
            known = self._templates.get(source.name)
        if known is not None and known[1] > now and not refresh:
            return known[0]
        with self._tilejson_fetch:  # one request for all the tiles waiting for it
            with self._templates_lock:
                latest = self._templates.get(source.name)
            if latest is not None and latest is not known and latest[1] > time.time():
                return latest[0]
            if time.time() - self._tilejson_failed.get(source.name, 0.0) < TILEJSON_RETRY:
                return known[0] if known else None  # it just failed for another tile
            return self._read_tilejson(source, known)

    def _read_tilejson(self, source: OnlineSource, known: tuple[str, float] | None) -> str | None:
        now = time.time()
        try:
            response = self.http.get(source.tilejson, timeout=source.timeout)
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            template = response.json()["tiles"][0]
            # Same scheme as the TileJSON itself (https for OpenFreeMap).
            scheme = source.tilejson.split("://", 1)[0]
            if not template.startswith(f"{scheme}://") or "{z}" not in template:
                raise ValueError(f"unexpected tile URL {template!r}")
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            if isinstance(exc, requests.ConnectionError):
                self.status.network_down()
            self._tilejson_failed[source.name] = time.time()
            self._failure(source)
            log.info("%s: could not read %s: %s", source.name, source.tilejson, exc)
            return known[0] if known else None  # an older template still works for a while
        expires, _ = expiry_from_headers(response.headers, now)
        valid_until = now + min(max(expires - now, TILEJSON_TTL[0]), TILEJSON_TTL[1])
        with self._templates_lock:
            self._templates[source.name] = (template, valid_until)
        if known is None or known[0] != template:
            log.info("%s tiles: %s", source.name, template)
        return template

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

    def _download(
        self, source: OnlineSource, z: int, x: int, y: int, cached: CachedTile | None, retry: bool = True
    ) -> bytes | None:
        stale = cached.data if cached else None
        template = self._template(source)
        if template is None:
            return stale
        url = template.format(z=z, x=x, y=y)
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
            if source.tilejson and retry:
                # The weekly version in the URL may be gone: read the TileJSON again.
                if self._template(source, refresh=True) not in (None, template):
                    return self._download(source, z, x, y, cached, retry=False)
            self._success(source)
            return None
        if response.status_code != 200 or not response.content:
            self._failure(source)
            return stale
        content_type = response.headers.get("Content-Type", "")
        expected = VECTOR_TYPES if source.kind == "vector" else ("image/",)
        if content_type and not content_type.startswith(expected):
            self._failure(source)
            return stale
        expires, storable = expiry_from_headers(response.headers, now)
        if source.max_ttl is not None:
            expires = min(expires, now + source.max_ttl)
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
