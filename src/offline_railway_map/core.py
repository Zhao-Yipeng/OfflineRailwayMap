"""Backend wiring: services, API routes and lifecycle.

The backend has no Qt dependency so it can run headless (``--serve``) and be
driven by a normal browser in tests.  The window shell supplies ``ShellHooks``
for the few things only it can do.
"""

from __future__ import annotations

import logging
import webbrowser
from urllib.parse import urlsplit

from . import APP_NAME, __version__
from .events import EventBus, frame
from .paths import Home, web_dir
from .server import LocalServer, Request, Response, Router, error_response, json_response
from .settings import Settings

log = logging.getLogger(__name__)


class ShellHooks:
    """Requests from the backend to the window shell.

    These defaults are used when the backend runs without a window.
    """

    def open_external(self, url: str) -> None:
        webbrowser.open(url)

    def fatal(self, reason: str) -> None:
        log.error("page reported a fatal problem: %s", reason)


class AppCore:
    def __init__(self, home: Home, settings: Settings, hooks: ShellHooks | None = None, port: int = 0) -> None:
        self.home = home
        self.settings = settings
        self.hooks = hooks or ShellHooks()
        self.events = EventBus()
        self.router = Router()
        self.server = LocalServer(self.router, web_dir(), port=port)
        self._register_routes()

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> str:
        """Start serving; return the URL of the page."""
        self.server.start()
        return f"{self.server.base_url}/app/index.html"

    def shutdown(self) -> None:
        self.events.close()
        self.server.stop()
        self.settings.save_if_data_exists(self.home)

    # -- routes ----------------------------------------------------------------------

    def _register_routes(self) -> None:
        add = self.router.add
        add("GET", "/api/info", self._api_info)
        add("POST", "/api/view", self._api_view)
        add("POST", "/api/open-external", self._api_open_external)
        add("POST", "/api/fatal", self._api_fatal)
        add("GET", "/api/events", self._api_events)

    def _api_info(self, _req: Request) -> Response:
        lon, lat, zoom = self.settings.initial_view()
        return json_response(
            {
                "app": APP_NAME,
                "version": __version__,
                "origin": self.server.origin,
                "base": f"/{self.server.token}",
                "view": {"lon": lon, "lat": lat, "zoom": zoom},
                "settings": self.settings.public_dict(),
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

    def _api_events(self, _req: Request) -> Response:
        initial = [frame("hello", {"version": __version__})]
        return Response(
            200,
            content_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-store"},
            stream=self.events.stream(initial),
        )
