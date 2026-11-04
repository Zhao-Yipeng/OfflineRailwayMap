"""Backend wiring: services, API routes and lifecycle.

The backend has no window dependency so it can run headless (``--serve``) and
be driven by a normal browser in tests.  The window shell supplies
``ShellHooks`` for the few things only it can do.
"""

from __future__ import annotations

import logging
import webbrowser
from urllib.parse import urlsplit

from . import APP_NAME, __version__
from .basemap import BasemapFetcher, BasemapTiles, BuildSelector, LowzoomStore, install_ancestors
from .cells import CellService
from .coverage import CellCoverage
from .downloader import Downloader
from .events import EventBus, frame
from .net import Http, NetStatus
from .online_tiles import OnlineTiles
from .overpass import OverpassClient
from .paths import Home, web_dir
from .rail_tiles import RailTiles
from .server import LocalServer, Request, Response, Router, error_response, json_response
from .settings import Settings
from .store import StateStore

log = logging.getLogger(__name__)

PNG_HEADERS = {"Cache-Control": "no-store"}


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
    ) -> None:
        self.home = home
        self._downloads = downloads  # tests switch the background downloader off
        self.settings = settings
        self.hooks = hooks or ShellHooks()
        self.events = EventBus()
        self.http = Http(per_host=2)
        self.status = NetStatus(self.events)
        self.coverage = CellCoverage()
        self.online = OnlineTiles(home, self.http, self.status, self.coverage, lambda: self.settings.online_cache_limit_mb)
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
        self.downloader = Downloader(
            home, self.store, self.cells, self.overpass, self.rail, self.status, basemap=self.basemap
        )
        self.downloader.on_install(lambda cid, cell_dir: install_ancestors(cell_dir, cid, self.lowzoom))
        self.cells.on_queue(self.downloader.wake)
        self.cells.on_delete(self._cells_deleted)

        self.router = Router()
        self.server = LocalServer(self.router, web_dir(), port=port)
        self.online_router = Router()
        self.online_server = LocalServer(self.online_router, None, token=self.server.token)
        self._register_routes()

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> str:
        """Start serving; return the URL of the page."""
        self.cells.start()
        self.rail.load_versions(self.store.cells())
        self.server.start()
        self.online_server.start()
        self.online_server.cors_origin = self.server.origin
        if self._downloads:
            self.downloader.start()
        return f"{self.server.base_url}/app/index.html"

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
        add("GET", r"/tiles/rail/(?P<z>\d{1,2})/(?P<x>\d{1,7})/(?P<y>\d{1,7})\.mvt", self._tile_rail)
        add("GET", r"/tiles/basemap/(?P<z>\d{1,2})/(?P<x>\d{1,7})/(?P<y>\d{1,7})\.mvt", self._tile_basemap)
        self.online_router.add(
            "GET", r"/tiles/(?P<source>osm|orm)/(?P<z>\d{1,2})/(?P<x>\d{1,7})/(?P<y>\d{1,7})\.png", self._tile_online
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

    def _tile_rail(self, req: Request) -> Response:
        p = req.params
        data = self.rail.tile(int(p["z"]), int(p["x"]), int(p["y"]))
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
        return Response(200, data, "image/png", PNG_HEADERS)
