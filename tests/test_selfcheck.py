"""The built program's --self-check (SPEC §17.1) works and leaves nothing behind."""

from __future__ import annotations

import tempfile

from offline_railway_map import selfcheck


def test_self_check_passes_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    report = tmp_path / "report.txt"
    assert selfcheck.main(["OfflineRailwayMap.exe", "--self-check", str(report)]) == 0
    assert report.read_text(encoding="utf-8").startswith("ok:")
    assert [p.name for p in tmp_path.iterdir()] == ["report.txt"]
