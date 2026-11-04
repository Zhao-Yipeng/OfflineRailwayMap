"""Search (SPEC §6.7, §12.2, §12.6).

* local stations of cached cells (works offline);
* stations via the OpenRailwayMap API (5 s timeout, 429 -> pause 10 min);
* places via Nominatim (at most 1 request per second, results cached, only
  on Enter - no autocomplete).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

import requests

from .net import NetStatus
from .store import StateStore

log = logging.getLogger(__name__)

ORM_API = "https://api.openrailwaymap.org/v2/facility"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
ORM_TIMEOUT = 5.0
NOMINATIM_TIMEOUT = 10.0
CACHE_SIZE = 100


class SearchService:
    def __init__(self, session: requests.Session, store: StateStore, status: NetStatus) -> None:
        self.session = session
        self.store = store
        self.status = status
        self._cache: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
        self._lock = threading.Lock()
        self._nominatim_lock = threading.Lock()
        self._nominatim_last = 0.0
        self._orm_paused_until = 0.0

    def search(self, text: str) -> dict:
        text = text.strip()[:200]
        result = {"query": text, "local": self.store.search_stations(text) if text else [], "orm": None,
                  "places": None, "offline": not self.status.online, "errors": {}}
        if not text or not self.status.online:
            return result
        try:
            result["orm"] = self._cached("orm", text, self._orm)
        except Exception as exc:  # noqa: BLE001 - shown to the user, search continues
            result["errors"]["orm"] = str(exc)
        try:
            result["places"] = self._cached("places", text, self._nominatim)
        except Exception as exc:  # noqa: BLE001
            result["errors"]["places"] = str(exc)
        return result

    def _cached(self, kind: str, text: str, fetch) -> list[dict]:
        key = (kind, text.lower())
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        value = fetch(text)
        with self._lock:
            self._cache[key] = value
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return value

    def _orm(self, text: str) -> list[dict]:
        if time.time() < self._orm_paused_until:
            raise RuntimeError("OpenRailwayMap 搜索暂停中（请求过多）")
        try:
            response = self.session.get(ORM_API, params={"q": text, "limit": 20}, timeout=ORM_TIMEOUT)
        except requests.RequestException as exc:
            raise RuntimeError("OpenRailwayMap 搜索不可用") from exc
        if response.status_code == 429:
            self._orm_paused_until = time.time() + 600
            raise RuntimeError("OpenRailwayMap 搜索请求过多，10 分钟后再试")
        response.raise_for_status()
        items = []
        for f in response.json():
            lat, lon = f.get("latitude"), f.get("longitude")
            if lat is None or lon is None:
                continue
            items.append({
                "name": f.get("name") or f.get("ref") or "",
                "kind": f.get("railway") or "",
                "operator": f.get("operator") or "",
                "lon": float(lon),
                "lat": float(lat),
            })
        return items

    def _nominatim(self, text: str) -> list[dict]:
        with self._nominatim_lock:  # SPEC §12.6: at most one request per second
            wait = self._nominatim_last + 1.0 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.get(
                    NOMINATIM, params={"q": text, "format": "jsonv2", "limit": 10}, timeout=NOMINATIM_TIMEOUT
                )
            except requests.RequestException as exc:
                raise RuntimeError("地名搜索不可用") from exc
            finally:
                self._nominatim_last = time.monotonic()
        response.raise_for_status()
        items = []
        for place in response.json():
            box = place.get("boundingbox")
            items.append({
                "name": place.get("display_name", ""),
                "kind": place.get("type") or place.get("category") or "",
                "lon": float(place["lon"]),
                "lat": float(place["lat"]),
                # Nominatim: [south, north, west, east] -> [west, south, east, north]
                "bbox": [float(box[2]), float(box[0]), float(box[3]), float(box[1])] if box else None,
            })
        return items
