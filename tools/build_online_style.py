"""Online basemap layers (SPEC §7.2): OpenFreeMap "Positron" (OpenMapTiles
schema), recoloured to the palette of the offline basemap (Protomaps
"grayscale", SPEC §7.7) so that cached and uncached areas look alike.

    python tools/build_online_style.py <positron style.json> <output layers.json>

Normally run by ``tools/vendor_assets.py online-style``.  Every Positron layer
must be listed in ``LAYERS`` (recoloured or dropped): an upstream change makes
this script stop instead of silently producing an unreviewed style.

* place names use the OSM ``name`` tag (local language), like the offline basemap;
* fonts are the bundled Noto Sans stacks ("Bold" -> "Medium");
* icons come from the bundled Protomaps sprite (town dots); road shields,
  which need OpenFreeMap's sprite, are left out;
* layer ids get an ``ofm-`` prefix and use the page's ``online`` source.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

SOURCE = "online"
PREFIX = "ofm-"
LOCAL_NAME = ["coalesce", ["get", "name"], ""]
FONTS = {"Noto Sans Regular": "Noto Sans Regular", "Noto Sans Bold": "Noto Sans Medium", "Noto Sans Italic": "Noto Sans Italic"}
ICONS = {"circle_11_black": "townspot"}
ALLOWED_ICONS = {"townspot", "capital", ""}

# The offline basemap's colours (web/vendor/basemap/grayscale-layers.json).
EARTH = "#cccccc"  # land; OpenMapTiles has no land polygons, so it is the page background
PARK = "#c2c2c2"
GLACIER = "#d2d2d2"
WATER = "#a3a3a3"
BUILDING = "#e0e0e0"
PIER = "#b8b8b8"
RUNWAY = "#f5f5f5"
TUNNEL_CASING = "#b8b8b8"
TUNNEL = "#d6d6d6"
CASING = EARTH
MAJOR = "#ebebeb"
OTHER = "#e0e0e0"
MINOR = ["interpolate", ["exponential", 1.6], ["zoom"], 11, MAJOR, 16, OTHER]
MOTORWAY = ["interpolate", ["linear"], ["zoom"], 5.8, "rgba(235,235,235,0.53)", 6, MAJOR]
BOUNDARY = "#5c5c5c"
BOUNDARY_DASH = ["step", ["zoom"], ["literal", [2, 0]], 4, ["literal", [2, 1]]]
LABEL_PLACE = {"text-color": "#474747", "text-halo-color": EARTH}
LABEL_SUBPLACE = {"text-color": "#7a7a7a", "text-halo-color": EARTH}
LABEL_REGION = {"text-color": "#999999", "text-halo-color": EARTH}
LABEL_COUNTRY = {"text-color": "#858585", "text-halo-color": EARTH}
LABEL_WATER = {"text-color": "#7a7a7a", "text-halo-color": WATER}

# Positron layer id -> paint overrides; None drops the layer.
LAYERS: dict[str, dict | None] = {
    "background": None,  # the page's own background has the land colour
    "park": {"fill-color": PARK},
    "water": {"fill-color": WATER},
    "landcover_ice_shelf": {"fill-color": GLACIER},
    "landcover_glacier": {"fill-color": GLACIER},
    "landuse_residential": None,  # not shaded in the offline basemap either
    "landcover_wood": {"fill-color": PARK},
    "waterway": {"line-color": WATER},
    "building": {"fill-color": BUILDING, "fill-outline-color": "#d4d4d4"},
    "tunnel_motorway_casing": {"line-color": TUNNEL_CASING},
    "tunnel_motorway_inner": {"line-color": TUNNEL},
    "aeroway-taxiway": {"line-color": RUNWAY},
    "aeroway-runway-casing": {"line-color": OTHER},
    "aeroway-area": {"fill-color": "#e6e6e6"},
    "aeroway-runway": {"line-color": RUNWAY},
    "road_area_pier": {"fill-color": PIER},
    "road_pier": {"line-color": PIER},
    "highway_path": {"line-color": OTHER},
    "highway_minor": {"line-color": MINOR},
    "highway_major_casing": {"line-color": CASING},
    "highway_major_inner": {"line-color": MAJOR},
    "highway_major_subtle": {"line-color": "rgba(235,235,235,0.69)"},
    "highway_motorway_casing": {"line-color": CASING},
    "highway_motorway_inner": {"line-color": MOTORWAY},
    "highway_motorway_subtle": {"line-color": "rgba(235,235,235,0.53)"},
    "railway_transit": {"line-color": OTHER},
    "railway_transit_dashline": {"line-color": RUNWAY},
    "railway_service": {"line-color": OTHER},
    "railway_service_dashline": {"line-color": RUNWAY},
    "railway": {"line-color": OTHER},
    "railway_dashline": {"line-color": RUNWAY},
    "highway_motorway_bridge_casing": {"line-color": CASING},
    "highway_motorway_bridge_inner": {"line-color": MOTORWAY},
    "boundary_3": {"line-color": BOUNDARY, "line-width": 0.4, "line-dasharray": BOUNDARY_DASH},
    "boundary_2": {"line-color": BOUNDARY, "line-width": 0.7, "line-dasharray": BOUNDARY_DASH},
    "boundary_disputed": {"line-color": BOUNDARY, "line-width": 0.7},
    "waterway_line_label": LABEL_WATER,
    "water_name_point_label": LABEL_WATER,
    "water_name_line_label": LABEL_WATER,
    "highway-name-path": {"text-color": "#999999", "text-halo-color": OTHER},
    "highway-name-minor": {"text-color": "#999999", "text-halo-color": OTHER},
    "highway-name-major": {"text-color": "#8f8f8f", "text-halo-color": MAJOR},
    "highway-shield-non-us": None,  # shields need OpenFreeMap's sprite
    "highway-shield-us-interstate": None,
    "road_shield_us": None,
    "airport": LABEL_SUBPLACE,
    "label_other": LABEL_SUBPLACE,
    "label_village": LABEL_PLACE,
    "label_town": LABEL_PLACE,
    "label_state": LABEL_REGION,
    "label_city": LABEL_PLACE,
    "label_city_capital": LABEL_PLACE,
    "label_country_3": LABEL_COUNTRY,
    "label_country_2": LABEL_COUNTRY,
    "label_country_1": LABEL_COUNTRY,
}
CAPITAL_LAYERS = {"label_city_capital"}  # town dot -> the offline style's capital icon
NO_ICON_LAYERS = {"airport"}  # Positron's airport icon is not in the bundled sprite


def _mentions_name(expression) -> bool:
    text = json.dumps(expression)
    return '"name' in text


def _replace_icon(value, mapping: dict[str, str]):
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_replace_icon(v, mapping) for v in value]
    return value


def _icons_used(value) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and value and value[0] == "step":
        # ["step", ["zoom"], default, stop, value, ...]
        return {v for v in value[2::2] if isinstance(v, str)}
    if isinstance(value, list):
        out: set[str] = set()
        for v in value:
            out |= _icons_used(v)
        return out
    return set()


def convert(style: dict) -> list[dict]:
    upstream = [layer["id"] for layer in style["layers"]]
    unknown = [lid for lid in upstream if lid not in LAYERS]
    missing = [lid for lid in LAYERS if lid not in upstream]
    if unknown or missing:
        raise SystemExit(f"Positron layers changed upstream; review LAYERS (new: {unknown}, gone: {missing})")
    out = []
    for upstream_layer in style["layers"]:
        overrides = LAYERS[upstream_layer["id"]]
        if overrides is None:
            continue
        layer = copy.deepcopy(upstream_layer)
        layer.pop("metadata", None)
        lid = layer["id"]
        layer["id"] = PREFIX + lid
        if layer.get("source") != "openmaptiles":
            raise SystemExit(f"{lid}: unexpected source {layer.get('source')!r}")
        layer["source"] = SOURCE
        paint = layer.setdefault("paint", {})
        paint.update(copy.deepcopy(overrides))
        layout = layer.get("layout", {})
        if "text-field" in layout and _mentions_name(layout["text-field"]):
            layout["text-field"] = LOCAL_NAME
        if "text-font" in layout:
            fonts = layout["text-font"]
            if not isinstance(fonts, list) or any(f not in FONTS for f in fonts):
                raise SystemExit(f"{lid}: unexpected fonts {fonts!r}")
            layout["text-font"] = [FONTS[f] for f in fonts]
        if lid in NO_ICON_LAYERS:
            for key in [k for k in layout if k.startswith("icon-")]:
                del layout[key]
            layout.pop("text-optional", None)
            layout.pop("text-offset", None)
            layout["text-anchor"] = "center"
        elif "icon-image" in layout:
            mapping = {"circle_11_black": "capital"} if lid in CAPITAL_LAYERS else ICONS
            layout["icon-image"] = _replace_icon(layout["icon-image"], mapping)
            if isinstance(layout.get("icon-size"), (int, float)):
                # Positron's dot is 11 px and drawn small; the town dot is 10 px.
                layout["icon-size"] = round(min(0.7, layout["icon-size"] * 1.75), 2)
            icons = _icons_used(layout["icon-image"])
            if not icons <= ALLOWED_ICONS:
                raise SystemExit(f"{lid}: icons not in the bundled sprite: {sorted(icons - ALLOWED_ICONS)}")
        out.append(layer)
    text = json.dumps(out)
    for leftover in ('"name:latin"', '"name:nonlatin"', '"name_en"', '"name_de"', "__TILEJSON_DOMAIN__"):
        if leftover in text:
            raise SystemExit(f"left over in the converted style: {leftover}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    style = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    layers = convert(style)
    Path(argv[1]).write_text(json.dumps(layers, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(layers)} layers -> {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
