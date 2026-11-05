"""User settings, stored in ``data/settings.ini`` (SPEC §6.10, §11.2).

Settings are only written when the user changes them, or on exit when the
data folder already exists.  Nothing is written to the Windows registry.
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .paths import Home

log = logging.getLogger(__name__)

# The free public instances with worldwide data (OSM wiki, Overpass API).
# overpass-api.de says it is overloaded and asks to use the others if
# possible, so it comes last (SPEC §12.4).
DEFAULT_OVERPASS_ENDPOINTS = (
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)
BASEMAP_ZOOM_CHOICES = (12, 13, 14, 15)
DEFAULT_VIEW = (105.0, 35.0, 4.0)  # SPEC §6.2: China-centred z4
CACHE_LIMIT_RANGE_MB = (256, 20480)


def _is_http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


@dataclass
class Settings:
    basemap_maxzoom: int = 14
    overpass_endpoints: list[str] = field(default_factory=lambda: list(DEFAULT_OVERPASS_ENDPOINTS))
    basemap_source: str = "auto"  # "auto" or the URL of a PMTiles archive
    online_cache_limit_mb: int = 2048
    show_cached_outline: bool = True
    always_show_construction: bool = False  # 「永远显示在建线路」 (SPEC §7.9)
    window_geometry: str = ""  # base64 of QMainWindow.saveGeometry()
    last_view: tuple[float, float, float] | None = None  # lon, lat, zoom

    # -- persistence -----------------------------------------------------------

    @classmethod
    def load(cls, home: Home) -> "Settings":
        settings = cls()
        path = home.settings_file
        if not path.is_file():
            return settings
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            log.warning("ignoring unreadable settings file %s", path)
            return settings
        general = parser["general"] if parser.has_section("general") else {}
        ui = parser["ui"] if parser.has_section("ui") else {}
        try:
            settings.apply_user_changes(
                {
                    "basemap_maxzoom": general.get("basemap_maxzoom", settings.basemap_maxzoom),
                    "overpass_endpoints": [
                        line.strip()
                        for line in str(general.get("overpass_endpoints", "")).splitlines()
                        if line.strip()
                    ]
                    or settings.overpass_endpoints,
                    "basemap_source": general.get("basemap_source", settings.basemap_source),
                    "online_cache_limit_mb": general.get("online_cache_limit_mb", settings.online_cache_limit_mb),
                    "show_cached_outline": str(ui.get("show_cached_outline", "true")).lower() == "true",
                    "always_show_construction": str(ui.get("always_show_construction", "false")).lower() == "true",
                }
            )
        except ValueError as exc:
            log.warning("invalid value in settings file, using defaults: %s", exc)
            settings = cls()
        settings.window_geometry = str(ui.get("window_geometry", ""))
        view = str(ui.get("last_view", ""))
        try:
            lon, lat, zoom = (float(v) for v in view.split(","))
            if -180 <= lon <= 180 and -86 <= lat <= 86 and 2 <= zoom <= 19:
                settings.last_view = (lon, lat, zoom)
        except ValueError:
            pass
        return settings

    def save(self, home: Home) -> None:
        """Write the settings file, creating the data folder if needed."""
        home.ensure_data()
        parser = configparser.ConfigParser(interpolation=None)
        parser["general"] = {
            "basemap_maxzoom": str(self.basemap_maxzoom),
            "overpass_endpoints": "\n".join(self.overpass_endpoints),
            "basemap_source": self.basemap_source,
            "online_cache_limit_mb": str(self.online_cache_limit_mb),
        }
        ui = {
            "show_cached_outline": "true" if self.show_cached_outline else "false",
            "always_show_construction": "true" if self.always_show_construction else "false",
        }
        if self.window_geometry:
            ui["window_geometry"] = self.window_geometry
        if self.last_view:
            ui["last_view"] = ",".join(f"{v:.6f}" for v in self.last_view)
        parser["ui"] = ui
        path = home.settings_file
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            parser.write(fh)
        os.replace(tmp, path)

    def save_if_data_exists(self, home: Home) -> None:
        """Persist UI state only when the data folder is already there (SPEC D12)."""
        if home.data_exists() and not home.blocked:
            try:
                self.save(home)
            except OSError:
                log.exception("could not save settings")

    # -- user-editable values ------------------------------------------------------

    def public_dict(self) -> dict:
        return {
            "basemapMaxzoom": self.basemap_maxzoom,
            "overpassEndpoints": list(self.overpass_endpoints),
            "basemapSource": self.basemap_source,
            "onlineCacheLimitMb": self.online_cache_limit_mb,
            "showCachedOutline": self.show_cached_outline,
            "alwaysShowConstruction": self.always_show_construction,
        }

    def apply_user_changes(self, values: dict) -> None:
        """Validate and apply editable values; raise ValueError on bad input.

        Accepts both the snake_case names used in the file and the camelCase
        names used by the page.
        """

        def pick(snake: str, camel: str):
            if snake in values:
                return values[snake]
            return values.get(camel, getattr(self, snake))

        zoom = int(pick("basemap_maxzoom", "basemapMaxzoom"))
        if zoom not in BASEMAP_ZOOM_CHOICES:
            raise ValueError(f"basemap max zoom must be one of {BASEMAP_ZOOM_CHOICES}")
        endpoints = [str(e).strip() for e in pick("overpass_endpoints", "overpassEndpoints") if str(e).strip()]
        if not endpoints or not all(_is_http_url(e) for e in endpoints):
            raise ValueError("Overpass servers must be http(s) URLs")
        source = str(pick("basemap_source", "basemapSource")).strip() or "auto"
        if source != "auto" and not _is_http_url(source):
            raise ValueError("basemap source must be 'auto' or an http(s) URL")
        limit = int(pick("online_cache_limit_mb", "onlineCacheLimitMb"))
        low, high = CACHE_LIMIT_RANGE_MB
        if not low <= limit <= high:
            raise ValueError(f"online cache limit must be between {low} and {high} MB")
        outline = pick("show_cached_outline", "showCachedOutline")
        construction = pick("always_show_construction", "alwaysShowConstruction")
        self.basemap_maxzoom = zoom
        self.overpass_endpoints = endpoints
        self.basemap_source = source
        self.online_cache_limit_mb = limit
        self.show_cached_outline = bool(outline)
        self.always_show_construction = bool(construction)

    def initial_view(self) -> tuple[float, float, float]:
        return self.last_view or DEFAULT_VIEW
