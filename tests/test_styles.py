"""The bundled basemap styles only refer to bundled fonts and icons (SPEC §7.2, §7.7)."""

from __future__ import annotations

import json

import pytest

from offline_railway_map.paths import web_dir

BASEMAP = web_dir() / "vendor" / "basemap"
FONTS = web_dir() / "vendor" / "fonts"
SPRITE = json.loads((web_dir() / "vendor" / "sprites" / "v4" / "grayscale.json").read_text(encoding="utf-8"))


def load(name: str) -> list[dict]:
    return json.loads((BASEMAP / name).read_text(encoding="utf-8"))


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


FONT_EXPRESSION_WORDS = {"literal", "case", "get", "min_zoom", "<="}


@pytest.mark.parametrize("name", ["online-layers.json", "grayscale-layers.json"])
def test_fonts_are_bundled(name):
    for layer in load(name):
        for font in strings(layer.get("layout", {}).get("text-font", [])):
            if font not in FONT_EXPRESSION_WORDS:
                assert (FONTS / font).is_dir(), f"{layer['id']}: font {font!r} is not bundled"


def test_online_icons_are_in_the_bundled_sprite():
    for layer in load("online-layers.json"):
        icon = layer.get("layout", {}).get("icon-image")
        if icon is None:
            continue
        for name in strings(icon):
            if name not in ("step", "zoom", ""):
                assert name in SPRITE, f"{layer['id']}: icon {name!r} is not in the sprite"


def test_online_layers_use_the_online_source_and_local_names():
    layers = load("online-layers.json")
    assert len(layers) > 30
    for layer in layers:
        assert layer["id"].startswith("ofm-")
        assert layer["source"] == "online"
        text = layer.get("layout", {}).get("text-field")
        if text is not None:
            assert text == ["coalesce", ["get", "name"], ""], f"{layer['id']}: local names only"
    dump = json.dumps(layers)
    assert "name:latin" not in dump and "name_en" not in dump
    assert "openfreemap.org" not in dump, "everything goes through the program's own server"


def test_online_palette_matches_the_offline_basemap():
    offline = {layer["id"]: layer for layer in load("grayscale-layers.json")}
    online = {layer["id"]: layer for layer in load("online-layers.json")}
    assert online["ofm-water"]["paint"]["fill-color"] == offline["water"]["paint"]["fill-color"]
    assert online["ofm-park"]["paint"]["fill-color"] == offline["landuse_urban_green"]["paint"]["fill-color"]
    assert online["ofm-highway_major_inner"]["paint"]["line-color"] == offline["roads_major"]["paint"]["line-color"]
    assert online["ofm-label_city"]["paint"]["text-color"] == offline["places_locality"]["paint"]["text-color"]
    # Positron's own colours are gone
    dump = json.dumps(list(online.values()))
    for positron_colour in ("rgb(242,243,240)", "#495e91", "rgb(230, 233, 229)", "#fff\"", "#000\""):
        assert positron_colour not in dump
