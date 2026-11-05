"""Backend wiring: services, API routes and lifecycle.

The backend has no window dependency so it can run headless (``--serve``) and
be driven by a normal browser in tests.  The window shell supplies
``ShellHooks`` for the few things only it can do.
"""

from __future__ import annotations

import dataclasses
import logging
import random
import shutil
import threading
import webbrowser
from urllib.parse import urlsplit

from . import APP_NAME, __version__, cleanup, logs
from .basemap import BasemapFetcher, BasemapTiles, BuildSelector, LowzoomStore, install_ancestors
from .cells import CellService
from .construction import ConstructionLines
from .coverage import CellCoverage
from .downloader import Downloader
from .events import EventBus, frame
from .net import Http, NetStatus
from .extracts import ExtractStore
from .online_tiles import OnlineTiles
from .overpass import OverpassClient
from .paths import Home, web_dir
from .classify import classify
from .grid import cell_at, cell_id, parse_cell_id
from .speed import color_speed, speed_label
from .rail_tiles import RailTiles
from .search import SearchService
from .server import LocalServer, Request, Response, Router, error_response, json_response
from .settings import Settings
from .store import StateStore

log = logging.getLogger(__name__)

PNG_HEADERS = {"Cache-Control": "no-store"}
RAIL_BYTES_PER_CELL = 2 * 1024 * 1024  # SPEC §9.7 rough estimate
ESTIMATE_SAMPLE = 100


def describe_line(way_id: int, tags: dict) -> dict:
    """What the details popup shows besides the raw tags (SPEC §6.8)."""
    cls = classify(tags)
    return {
        "id": way_id,
        "tags": tags,
        "state": cls.state if cls else None,
        "kind": cls.kind if cls else tags.get("railway"),
        "usage": cls.usage if cls else tags.get("usage"),
        "service": cls.service if cls else tags.get("service"),
        "speedLabel": speed_label(tags),
        "colorSpeed": color_speed(tags),
    }


class ShellHooks:
    """Requests from the backend to the window shell.

    These defaults are used when the backend runs without a window.
    """

    def open_external(self, url: str) -> None:
        webbrowser.open(url)

    def fatal(self, reason: str) -> None:
        log.error("page reported a fatal problem: %s", reason)


class AppCore:
    def __init__(
        self,
        home: Home,
        settings: Settings,
        hooks: ShellHooks | None = None,
        port: int = 0,
        downloads: bool = True,
        online_sources: tuple | None = None,
    ) -> None:
        self.home = home
        self._downloads = downloads  # tests switch the background downloader off
        self.settings = settings
        self.hooks = hooks or ShellHooks()
        self.events = EventBus()
        self.http = Http(per_host=2)
        self.status = NetStatus(self.events)
        self.coverage = CellCoverage()
        self.online = OnlineTiles(
            home, self.http, self.status, self.coverage, lambda: self.settings.online_cache_limit_mb,
            **({"sources": online_sources} if online_sources else {}),
        )
        self.store = StateStore(home)
        self.cells = CellService(
            home, self.store, self.coverage, self.events, default_basemap_maxzoom=lambda: self.settings.basemap_maxzoom
        )
        self.rail = RailTiles(home, self.coverage)
        self.lowzoom = LowzoomStore(home)
        self.basemap_tiles = BasemapTiles(home, self.coverage, self.lowzoom, self.rail)
        self.basemap_selector = BuildSelector(self.http.session, lambda: self.settings.basemap_source)
        self.basemap = BasemapFetcher(self.http.session, self.basemap_selector, lambda: self.settings.basemap_maxzoom)
        self.overpass = OverpassClient(self.http.session, lambda: list(self.settings.overpass_endpoints), self.store)
        self.extracts = ExtractStore(home, self.http.session)
        self.downloader = Downloader(
            home, self.store, self.cells, self.overpass, self.rail, self.status, basemap=self.basemap,
            extract_store=self.extracts,
        )
        # Shares the extracts with cell downloads (SPEC §7.9): whatever is there or
        # being downloaded is used by both.
        self.construction = ConstructionLines(
            home, self.extracts, self.coverage, lambda: self.settings.always_show_construction, self.status,
            on_change=lambda version: self.events.publish("construction", {"version": version}),
            on_question=lambda question: self.events.publish("construction-question", question),
            planned_regions=self.downloader.planned_regions,
            view_center=lambda: self.settings.initial_view()[:2],
            cells_busy=lambda: self.downloader.busy,
        )
        self.downloader.on_install(lambda cid, cell_dir: install_ancestors(cell_dir, cid, self.lowzoom))
        self.cells.on_queue(self.downloader.queue_changed)
        self.cells.on_delete(self._cells_deleted)
        self.rail.on_corrupt = lambda cid: self.cells.mark_failed(cid, "数据已损坏，请重新下载")
        self.search = SearchService(self.http.session, self.store, self.status)
        self._clear_lock = threading.Lock()
        self._estimates: dict[str, int] = {}

        self.router = Router()
        self.server = LocalServer(self.router, web_dir(), port=port)
        self.online_router = Router()
        self.online_server = LocalServer(self.online_router, None, token=self.server.token)
        self._register_routes()

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> str:
        """Start serving; return the URL of the page."""
        self.extracts.remove_stale()
        self.cells.start()
        self.rail.load_versions(self.store.cells())
        self.server.start()
        self.online_server.start()
        self.online_server.cors_origin = self.server.origin
        if self._downloads:
            self.downloader.start()
            self.construction.start()
        return f"{self.server.base_url}/app/index.html"

    def clear_cache(self) -> list[str]:
        """「清空缓存」(SPEC §6.6): delete the whole data folder, reset in memory.

        Returns the paths that could not be deleted (empty on success).
        """
        with self._clear_lock:
            self.downloader.suspend()
            try:
                # While the gate is closed nothing opens or creates a file in
                # data\, so no file is locked or recreated during the deletion.
                self.extracts.stop()  # downloads and filtering of regional extracts
                self.construction.stop(timeout=1.0)
                with self.home.blocked_while_clearing():
                    # Online tiles go to memory from now on, so reloading the map
                    # after clearing does not recreate data\ (SPEC §6.6).
                    self.online.cache.memory_only = True
                    self.online.close()
                    self.rail.close_all()
                    self.rail.forget(list(self.rail._versions))
                    self.lowzoom.close()
                    self.construction.close()
                    self.store.close()
                    logs.detach_file()
                    failed = cleanup.remove_data(self.home)
                    defaults = Settings()
                    for field in dataclasses.fields(Settings):
                        setattr(self.settings, field.name, getattr(defaults, field.name))
                    self._estimates.clear()
                    self.overpass.forget_all()
                    self.extracts.forget()
                    self.construction.forget()
                    self.downloader.queue_changed()
                    self.cells.reset()
                self.events.publish("settings", self.settings.public_dict())
            finally:
                self.downloader.resume()
        return [str(p) for p in failed]

    def estimate(self, ids: list[str]) -> dict:
        """Rough download size for new cells (SPEC §9.7)."""
        cells = [parse_cell_id(c) for c in ids]
        sample = cells if len(cells) <= ESTIMATE_SAMPLE else random.sample(cells, ESTIMATE_SAMPLE)
        total, failures = 0, 0
        for x, y in sample:
            key = cell_id(x, y)
            if key not in self._estimates:
                try:
                    self._estimates[key] = self.basemap.estimate(x, y)
                except Exception:  # noqa: BLE001 - estimate is best effort
                    failures += 1
                    continue
            total += self._estimates[key]
        measured = len(sample) - failures
        basemap = int(total * len(cells) / measured) if measured else None
        return {
            "cells": len(cells),
            "basemapBytes": basemap,
            "railBytes": RAIL_BYTES_PER_CELL * len(cells),
            "bytes": (basemap or 0) + RAIL_BYTES_PER_CELL * len(cells),
            "sampled": len(sample) < len(cells),
            "free": shutil.disk_usage(self.home.root).free,
        }

    def _cells_deleted(self, ids: list[str]) -> None:
        self.downloader.cancel(ids)
        self.rail.forget(ids)
        for cid in ids:
            self.lowzoom.remove(cid)

    @property
    def allowed_origins(self) -> list[str]:
        return [self.server.origin, self.online_server.origin]

    def shutdown(self) -> None:
        self.downloader.stop()
        self.extracts.stop(timeout=5.0)
        self.construction.stop(timeout=2.0)
        self.construction.close()
        self.events.close()
        self.server.stop()
        self.online_server.stop()
        self.online.close()
        self.rail.close_all()
        self.lowzoom.close()
        self.store.close()
        self.settings.save_if_data_exists(self.home)

    # -- routes ----------------------------------------------------------------------

    def _register_routes(self) -> None:
        add = self.router.add
        add("GET", "/api/info", self._api_info)
        add("POST", "/api/view", self._api_view)
        add("POST", "/api/open-external", self._api_open_external)
        add("POST", "/api/fatal", self._api_fatal)
        add("GET", "/api/events", self._api_events)
        add("GET", "/api/cells", lambda _req: json_response(self.cells.snapshot()))
        add("POST", "/api/cells/apply", self._api_cells_apply)
        add("POST", "/api/cells/delete", self._api_cells_delete)
        add("POST", "/api/cells/retry", self._api_cells_retry)
        add("POST", "/api/cells/update", self._api_cells_update)
        add("POST", "/api/cells/update-all", lambda _req: json_response({"count": self.cells.update_all_rail()}))
        add("POST", "/api/downloads/pause", self._api_pause)
        add("POST", "/api/clear", self._api_clear)
        add("POST", "/api/estimate", self._api_estimate)
        add("GET", "/api/search", lambda req: json_response(self.search.search(req.query.get("q", ""))))
        add("GET", "/api/details", self._api_details)
        add("GET", "/api/details/online", self._api_details_online)
        add("GET", "/api/settings", self._api_settings)
        add("GET", "/api/construction", lambda _req: json_response({"question": self.construction.question()}))
        add("POST", "/api/construction/decide", self._api_construction_decide)
        add("POST", "/api/settings", self._api_settings_save)
        add("GET", r"/tiles/rail/(?P<z>\d{1,2})/(?P<x>\d{1,7})/(?P<y>\d{1,7})\.mvt", self._tile_rail)
        add("GET", r"/tiles/basemap/(?P<z>\d{1,2})/(?P<x>\d{1,7})/(?P<y>\d{1,7})\.mvt", self._tile_basemap)
        add("GET", r"/tiles/construction/(?P<z>\d{1,2})/(?P<x>\d{1,7})/(?P<y>\d{1,7})\.mvt", self._tile_construction)
        self.online_router.add(
            "GET", r"/tiles/(?P<source>orm)/(?P<z>\d{1,2})/(?P<x>\d{1,7})/(?P<y>\d{1,7})\.png", self._tile_online
        )
        self.online_router.add(
            "GET", r"/tiles/(?P<source>ofm)/(?P<z>\d{1,2})/(?P<x>\d{1,7})/(?P<y>\d{1,7})\.pbf", self._tile_online
        )

    def _api_info(self, _req: Request) -> Response:
        lon, lat, zoom = self.settings.initial_view()
        return json_response(
            {
                "app": APP_NAME,
                "version": __version__,
                "base": f"/{self.server.token}",
                "onlineOrigin": self.online_server.origin,
                "view": {"lon": lon, "lat": lat, "zoom": zoom},
                "settings": self.settings.public_dict(),
                "status": self.status.snapshot(),
                "about": self._about(),
            }
        )

    def _api_view(self, req: Request) -> Response:
        data = req.json() or {}
        try:
            lon = max(-180.0, min(180.0, float(data["lon"])))
            lat = max(-85.0, min(85.0, float(data["lat"])))
            zoom = max(2.0, min(19.0, float(data["zoom"])))
        except (KeyError, TypeError, ValueError):
            return error_response(400, "bad view")
        self.settings.last_view = (lon, lat, zoom)
        return json_response({"ok": True})

    def _api_open_external(self, req: Request) -> Response:
        url = str((req.json() or {}).get("url", ""))
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return error_response(400, "only http(s) links can be opened")
        self.hooks.open_external(url)
        return json_response({"ok": True})

    def _api_fatal(self, req: Request) -> Response:
        reason = str((req.json() or {}).get("reason", "unknown"))
        self.hooks.fatal(reason)
        return json_response({"ok": True})

    @staticmethod
    def _ids(data, key: str = "ids") -> list[str]:
        value = (data or {}).get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{key} must be a list of cell ids")
        return value

    def _api_cells_apply(self, req: Request) -> Response:
        data = req.json() or {}
        try:
            add, remove = self._ids(data, "add"), self._ids(data, "remove")
            center = data.get("center")
            center_xy = (float(center["lon"]), float(center["lat"])) if center else None
            result = self.cells.apply(add, remove, center_xy)
        except (ValueError, KeyError, TypeError) as exc:
            return error_response(400, str(exc))
        return json_response({**result, **self.cells.snapshot()})

    def _api_cells_delete(self, req: Request) -> Response:
        try:
            removed = self.cells.delete(self._ids(req.json()))
        except ValueError as exc:
            return error_response(400, str(exc))
        return json_response({"removed": removed})

    def _api_cells_retry(self, req: Request) -> Response:
        try:
            queued = self.cells.retry(self._ids(req.json()))
        except ValueError as exc:
            return error_response(400, str(exc))
        return json_response({"queued": queued})

    def _api_cells_update(self, req: Request) -> Response:
        data = req.json() or {}
        try:
            ok = self.cells.request_update(str(data.get("id", "")), bool(data.get("basemap")))
        except ValueError as exc:
            return error_response(400, str(exc))
        return json_response({"ok": ok}) if ok else error_response(409, "cell is not cached")

    def _api_pause(self, req: Request) -> Response:
        self.cells.set_paused(bool((req.json() or {}).get("paused")))
        return json_response({"paused": self.cells.paused})

    def _api_events(self, _req: Request) -> Response:
        initial = [
            frame("hello", {"version": __version__}),
            frame("status", self.status.snapshot()),
            frame("cells", self.cells.snapshot()),
        ]
        return Response(
            200,
            content_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-store"},
            stream=self.events.stream(initial),
        )

    def _api_clear(self, _req: Request) -> Response:
        failed = self.clear_cache()
        return json_response({"ok": not failed, "failed": failed[:20]})

    def _api_estimate(self, req: Request) -> Response:
        try:
            return json_response(self.estimate(self._ids(req.json(), "cells")))
        except ValueError as exc:
            return error_response(400, str(exc))

    def _api_details(self, req: Request) -> Response:
        """Tags of a cached line (SPEC §6.8)."""
        try:
            cid = cell_id(*parse_cell_id(req.query.get("cell", "")))
            way_id = int(req.query.get("way", ""))
        except ValueError as exc:
            return error_response(400, str(exc))
        tags = self.rail.way_tags(cid, way_id)
        if tags is None:
            return error_response(404, "not found")
        row = self.store.cell(cid) or {}
        return json_response({**describe_line(way_id, tags), "cell": cid, "railAt": row.get("rail_at")})

    def _api_details_online(self, req: Request) -> Response:
        """Lines near a point outside the cached area, via Overpass (SPEC §6.8, P2)."""
        try:
            lon, lat = float(req.query["lon"]), float(req.query["lat"])
            radius = max(1.0, min(50.0, float(req.query.get("radius", 20))))
        except (KeyError, ValueError):
            return error_response(400, "bad point")
        if self.coverage.is_cached(*cell_at(lon, lat)):
            return json_response({"lines": []})
        try:
            lines = self.overpass.lines_near(lon, lat, radius)
        except Exception as exc:  # noqa: BLE001 - reported to the page
            return error_response(503, f"联网查询失败：{exc}")
        return json_response({"lines": [describe_line(line["id"], line["tags"]) for line in lines]})

    def _api_settings(self, _req: Request) -> Response:
        return json_response({"settings": self.settings.public_dict(), "about": self._about()})

    def _api_settings_save(self, req: Request) -> Response:
        try:
            self.settings.apply_user_changes(req.json() or {})
        except (ValueError, TypeError) as exc:
            return error_response(400, str(exc))
        self.settings.save(self.home)
        self.construction.settings_changed()
        self.events.publish("settings", self.settings.public_dict())
        return json_response({"settings": self.settings.public_dict()})

    @staticmethod
    def _about() -> dict:
        return {
            "app": APP_NAME,
            "version": __version__,
            "license": "GPL-3.0",
            "disclaimer": "OfflineRailwayMap 是独立的第三方程序，与 OpenRailwayMap 项目和 OpenStreetMap 基金会无关。",
            "components": [
                ["Qt / PySide6", "LGPL-3.0"],
                ["MapLibre GL JS 5.24.0", "BSD-3-Clause"],
                ["Protomaps basemaps 样式 5.7.2", "BSD-3-Clause"],
                ["Noto Sans 字体", "SIL OFL 1.1"],
                ["OpenRailwayMap-CartoCSS（速度颜色、线宽、显示规则）", "GPL-3.0-or-later"],
                ["shapely", "BSD-3-Clause"],
                ["pyosmium（osmium）", "BSD-2-Clause"],
                ["requests", "Apache-2.0"],
                ["mapbox-vector-tile", "MIT"],
                ["地图数据 © OpenStreetMap contributors", "ODbL"],
            ],
        }

    def _tile_rail(self, req: Request) -> Response:
        p = req.params
        data = self.rail.tile(int(p["z"]), int(p["x"]), int(p["y"]))
        return Response(200 if data else 204, data, "application/x-protobuf", PNG_HEADERS)

    def _api_construction_decide(self, req: Request) -> Response:
        """Answer to the question after the overlay's download (SPEC §7.9)."""
        data = req.json() or {}
        if not isinstance(data.get("delete"), bool):
            return error_response(400, "delete must be true or false")
        return json_response(self.construction.decide(data["delete"]))

    def _tile_construction(self, req: Request) -> Response:
        p = req.params
        z = int(p["z"])
        if not 2 <= z <= 14:
            return Response(204, b"", "application/x-protobuf", PNG_HEADERS)
        data = self.construction.tile(z, int(p["x"]), int(p["y"]))
        return Response(200 if data else 204, data, "application/x-protobuf", PNG_HEADERS)

    def _tile_basemap(self, req: Request) -> Response:
        p = req.params
        found = self.basemap_tiles.tile(int(p["z"]), int(p["x"]), int(p["y"]))
        if not found:
            return Response(204, b"", "application/x-protobuf", PNG_HEADERS)
        data, compression = found
        headers = dict(PNG_HEADERS)
        if compression == 2:
            headers["Content-Encoding"] = "gzip"
        return Response(200, data, "application/x-protobuf", headers)

    def _tile_online(self, req: Request) -> Response:
        p = req.params
        data = self.online.tile(p["source"], int(p["z"]), int(p["x"]), int(p["y"]))
        if self.online.sources[p["source"]].kind == "vector":
            return Response(200 if data else 204, data, "application/x-protobuf", PNG_HEADERS)
        return Response(200, data, "image/png", PNG_HEADERS)
