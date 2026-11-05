# Third-party notices / 第三方组件许可

OfflineRailwayMap is licensed under the GNU General Public License v3.0 (see `LICENSE`).
It includes or uses the following third-party components.

本程序以 GPL-3.0 发布，包含或使用了以下第三方组件。

## Bundled with the program / 随程序打包

| Component | Version | License | Notes |
|-----------|---------|---------|-------|
| Qt for Python (PySide6, shiboken6) and Qt 6 incl. Qt WebEngine | 6.8 | LGPL-3.0 (`licenses/LGPL-3.0.txt`) | Dynamically linked; the Qt libraries in `_internal\` can be replaced. Source: https://code.qt.io and https://download.qt.io/official_releases/QtForPython/ |
| Chromium (inside Qt WebEngine) | — | BSD-3-Clause and others | See the Qt WebEngine third-party licenses: https://doc.qt.io/qt-6/qtwebengine-licensing.html |
| MapLibre GL JS | 5.24.0 | BSD-3-Clause | `web/vendor/maplibre-gl/LICENSE.txt` |
| Protomaps basemaps style (generated "grayscale" layers) | 5.7.2 | BSD-3-Clause | `web/vendor/basemap/LICENSE.md` |
| Protomaps basemaps-assets: Noto Sans glyphs | 028c18f | SIL Open Font License 1.1 | `web/vendor/fonts/OFL.txt` |
| Protomaps basemaps-assets: sprites | 028c18f | see `web/vendor/basemap/LICENSE.md` | |
| OpenFreeMap "Positron" style (recoloured, as `web/vendor/basemap/online-layers.json`) | main, 2026-11 | MIT (OpenFreeMap changes); code BSD-3-Clause and design CC BY 4.0 (OpenMapTiles Positron GL Style, © KlokanTech.com & OpenMapTiles contributors); derived from "CartoDB Basemaps" designed by Stamen and Paul Norman for CartoDB Inc., CC BY 3.0 | `web/vendor/basemap/POSITRON-LICENSE.md`; https://github.com/hyperknot/openfreemap-styles |
| OpenRailwayMap-CartoCSS (speed colours, line widths, zoom rules and speed parsing ported to Python/JS) | master, 2026-11 | GPL-3.0-or-later | https://github.com/OpenRailwayMap/OpenRailwayMap-CartoCSS |
| Python | 3.12 | PSF License | https://docs.python.org/3/license.html |
| requests, urllib3, idna, charset-normalizer, certifi | — | Apache-2.0 / MIT / BSD-3-Clause / MIT / MPL-2.0 | |
| shapely, numpy | 2.x | BSD-3-Clause | |
| mapbox-vector-tile, pyclipper, protobuf | 2.x | MIT / MIT / BSD-3-Clause | |
| PyInstaller bootloader | 6.x | GPL-2.0 with bootloader exception | |

## Data and services / 数据与服务

| Source | Terms |
|--------|-------|
| Map data © OpenStreetMap contributors | Open Database License (ODbL) — https://www.openstreetmap.org/copyright |
| OpenRailwayMap tiles ("maxspeed" style) and search API | Tiles CC-BY-SA 2.0; terms of use: https://wiki.openstreetmap.org/wiki/OpenRailwayMap/API |
| OpenFreeMap vector tiles (online basemap, OpenMapTiles schema) | "OpenFreeMap © OpenMapTiles Data from OpenStreetMap"; terms of service: https://openfreemap.org/tos/ |
| Overpass API | https://wiki.openstreetmap.org/wiki/Overpass_API |
| Protomaps daily builds (offline basemap) | ODbL Produced Work — https://docs.protomaps.com/basemaps/downloads |
| Nominatim | https://operations.osmfoundation.org/policies/nominatim/ |

OfflineRailwayMap is an independent third-party program. It is not affiliated with the
OpenRailwayMap project or the OpenStreetMap Foundation.

OfflineRailwayMap 是独立的第三方程序，与 OpenRailwayMap 项目和 OpenStreetMap 基金会无关。
