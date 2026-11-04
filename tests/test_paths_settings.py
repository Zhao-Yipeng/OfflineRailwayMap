from __future__ import annotations

import pytest

from offline_railway_map import cleanup
from offline_railway_map.paths import Home
from offline_railway_map.settings import DEFAULT_OVERPASS_ENDPOINTS, Settings


def test_data_folder_is_created_only_on_demand(home: Home):
    assert not home.data_exists()
    events = []
    home.on_data_created(lambda: events.append("created"))
    home.ensure_data()
    home.ensure_data()
    assert home.data_exists()
    assert events == ["created"]


def test_write_probe_leaves_nothing(home: Home):
    before = sorted(p.name for p in home.root.iterdir())
    assert home.writable()
    assert sorted(p.name for p in home.root.iterdir()) == before


def test_settings_defaults_without_file(home: Home):
    settings = Settings.load(home)
    assert settings.basemap_maxzoom == 14
    assert settings.overpass_endpoints == list(DEFAULT_OVERPASS_ENDPOINTS)
    assert settings.initial_view() == (105.0, 35.0, 4.0)
    assert not home.data_exists()


def test_settings_roundtrip(home: Home):
    settings = Settings()
    settings.apply_user_changes({"basemapMaxzoom": 13, "showCachedOutline": False})
    settings.last_view = (116.4, 39.9, 9.5)
    settings.window_geometry = "AAAA"
    settings.save(home)
    loaded = Settings.load(home)
    assert loaded.basemap_maxzoom == 13
    assert loaded.show_cached_outline is False
    assert loaded.last_view == pytest.approx((116.4, 39.9, 9.5))
    assert loaded.window_geometry == "AAAA"


def test_ui_state_is_not_saved_without_data_folder(home: Home):
    settings = Settings()
    settings.last_view = (1.0, 2.0, 5.0)
    settings.save_if_data_exists(home)
    assert not home.data_exists()


@pytest.mark.parametrize(
    "change",
    [
        {"basemapMaxzoom": 16},
        {"overpassEndpoints": []},
        {"overpassEndpoints": ["ftp://example.org"]},
        {"basemapSource": "not a url"},
        {"onlineCacheLimitMb": 10},
    ],
)
def test_invalid_settings_are_rejected(change):
    settings = Settings()
    with pytest.raises(ValueError):
        settings.apply_user_changes(change)
    assert settings == Settings()


def test_corrupt_settings_file_falls_back_to_defaults(home: Home):
    home.ensure_data()
    home.settings_file.write_text("[general]\nbasemap_maxzoom = banana\n", encoding="utf-8")
    assert Settings.load(home).basemap_maxzoom == 14


def test_remove_data_deletes_everything(home: Home):
    (home.ensure_data() / "cells" / "1-2").mkdir(parents=True)
    (home.data / "cells" / "1-2" / "rail.sqlite").write_bytes(b"x")
    assert cleanup.remove_data(home) == []
    assert not home.data.exists()
    assert sorted(p.name for p in home.root.iterdir()) == []


def test_finish_pending_clear(home: Home):
    home.ensure_data()
    (home.data / "leftover.bin").write_bytes(b"x")
    home.clear_marker.touch()
    cleanup.finish_pending_clear(home)
    assert not home.data.exists()
