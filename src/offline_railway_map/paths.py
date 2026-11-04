"""Program folder and data folder layout (SPEC §11).

Everything the program writes lives in ``<program folder>/data``.  The data
folder is created only when something actually has to be stored, so starting
the program and closing it again leaves the program folder untouched.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

ENV_HOME = "OFFLINE_RAILWAY_MAP_HOME"


def program_dir() -> Path:
    """Folder that holds the program (and therefore the data folder)."""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Source checkout: <repo>/src/offline_railway_map/paths.py -> <repo>
    return Path(__file__).resolve().parents[2]


def web_dir() -> Path:
    """Bundled front-end files."""
    return Path(__file__).resolve().parent / "web"


class Home:
    """The program folder and its lazily created ``data`` folder."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.data = self.root / "data"
        self._on_data_created: list[Callable[[], None]] = []

    # -- data folder ---------------------------------------------------------

    def data_exists(self) -> bool:
        return self.data.is_dir()

    def on_data_created(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` whenever the data folder gets created."""
        self._on_data_created.append(callback)

    def ensure_data(self) -> Path:
        if not self.data.is_dir():
            self.data.mkdir(exist_ok=True)
            for callback in list(self._on_data_created):
                try:
                    callback()
                except Exception:  # a listener must never break the caller
                    log.exception("data folder listener failed")
        return self.data

    def ensure_dir(self, path: Path) -> Path:
        """Create ``path`` (inside the data folder) and its parents."""
        self.ensure_data()
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- well-known locations ------------------------------------------------

    @property
    def settings_file(self) -> Path:
        return self.data / "settings.ini"

    @property
    def state_db(self) -> Path:
        return self.data / "state.sqlite"

    @property
    def cells_dir(self) -> Path:
        return self.data / "cells"

    def cell_dir(self, cell_id: str) -> Path:
        return self.cells_dir / cell_id

    @property
    def lowzoom_db(self) -> Path:
        return self.data / "basemap_lowzoom.sqlite"

    @property
    def cache_dir(self) -> Path:
        return self.data / "cache"

    @property
    def tmp_dir(self) -> Path:
        return self.data / "tmp"

    @property
    def logs_dir(self) -> Path:
        return self.data / "logs"

    @property
    def clear_marker(self) -> Path:
        return self.data / ".clear_pending"

    # -- checks ----------------------------------------------------------------

    def writable(self) -> bool:
        """True if files can be created in the program folder.

        The probe file is removed again, so the check leaves nothing behind.
        """
        probe = self.root / f".write-test-{uuid.uuid4().hex}"
        try:
            with open(probe, "xb"):
                pass
        except OSError:
            return False
        try:
            probe.unlink()
        except OSError:
            log.warning("could not remove write probe %s", probe)
        return True
