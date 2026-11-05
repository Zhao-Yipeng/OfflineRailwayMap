"""Removing the data folder (SPEC §6.6, §11.3)."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import time
from pathlib import Path

from .paths import Home

log = logging.getLogger(__name__)


def remove_data(home: Home, attempts: int = 3, delay: float = 1.0) -> list[Path]:
    """Delete the whole data folder.

    Returns the paths that could not be removed (empty on success).  If some
    files stay locked after all attempts, an empty ``.clear_pending`` marker is
    left next to them so the next start finishes the job.
    """
    failed: list[Path] = []
    for attempt in range(attempts):
        if not home.data.exists():
            return []
        failed = []

        def retry_writable(fn, path, _exc) -> None:
            # Windows refuses to delete read-only files: clear the flag, try once more.
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                fn(path)
            except OSError:
                failed.append(Path(path))

        shutil.rmtree(home.data, onexc=retry_writable)
        if not home.data.exists():
            return []
        if attempt + 1 < attempts:
            time.sleep(delay)
    log.warning("could not delete %d path(s) in the data folder", len(failed))
    try:
        home.clear_marker.touch()
    except OSError:
        log.exception("could not write %s", home.clear_marker)
    return failed


def finish_pending_clear(home: Home) -> None:
    """At start-up, finish a clear that was interrupted by locked files."""
    if home.clear_marker.exists():
        log.info("finishing an interrupted cache clear")
        remove_data(home, attempts=2, delay=0.5)


def remove_tmp(home: Home) -> None:
    """Drop leftovers of downloads that were interrupted (SPEC §9.5)."""
    if home.tmp_dir.exists():
        shutil.rmtree(home.tmp_dir, ignore_errors=True)
