"""Background download queue (SPEC §9.1, §9.5, §9.6).

One cell at a time: railway data (Overpass) -> offline basemap (Protomaps,
if configured) -> processing -> atomic install.  Work happens in
``data/tmp/<cell>``; the cell only becomes "cached" once everything is in
place, so the page never shows half a cell.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from . import overpass, processing
from .cells import CellService
from .net import NetStatus
from .overpass import BudgetExhausted, OverpassClient, ServersBusy
from .paths import Home
from .rail_tiles import RailTiles
from .store import StateStore

log = logging.getLogger(__name__)

RAIL_SHARE = 0.55  # share of the progress bar for each stage (no basemap)
OFFLINE_RETRY = 60.0
BUSY_RETRY = 300.0
PUBLISH_INTERVAL = 0.5
PROGRESS_STEP = 0.05


class BasemapStage:
    """Interface for the offline basemap step (implemented in basemap.py)."""

    def fetch(
        self,
        x: int,
        y: int,
        work: Path,
        cancel: threading.Event,
        progress: Callable[[float], None],
        received: Callable[[int], None] | None = None,
    ) -> dict:
        return {}


class Downloader:
    def __init__(
        self,
        home: Home,
        store: StateStore,
        cells: CellService,
        client: OverpassClient,
        rail_tiles: RailTiles,
        status: NetStatus,
        basemap: BasemapStage | None = None,
    ) -> None:
        self.home = home
        self.store = store
        self.cells = cells
        self.client = client
        self.rail_tiles = rail_tiles
        self.status = status
        self.basemap = basemap
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._suspended = threading.Event()  # set while the data folder is being cleared
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self.min_free_bytes = 1024**3  # SPEC §9.7: pause below 1 GB free
        self.free_bytes: Callable[[], int] = lambda: shutil.disk_usage(self.home.root).free
        self._current: tuple[str, threading.Event] | None = None
        self._current_lock = threading.Lock()
        self._last_publish = 0.0
        self._last_progress = 0.0
        self._last_progress_value = 0.0
        self._last_job: str | None = None  # the cell to tell about a wait before the next attempt
        self._on_install: list[Callable[[str, Path], None]] = []

    def on_install(self, callback: Callable[[str, Path], None]) -> None:
        """Run ``callback(cell_id, cell_dir)`` right after a cell is installed."""
        self._on_install.append(callback)

    # -- control ------------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="downloader", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, ids: list[str]) -> None:
        with self._current_lock:
            if self._current and self._current[0] in ids:
                self._current[1].set()
        for cid in ids:
            x, y = (int(v) for v in cid.split("-"))
            self.client.forget_cell(x, y)
            self.cells.clear_live(cid)
            if self._last_job == cid:
                self._last_job = None

    def cancel_all(self) -> None:
        with self._current_lock:
            if self._current:
                self._current[1].set()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        self.cancel_all()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def busy(self) -> bool:
        return self._current is not None

    def suspend(self, timeout: float = 20.0) -> bool:
        """Stop working (cancel the current cell) until ``resume``; True when idle."""
        self._suspended.set()
        self.cancel_all()
        self._wake.set()
        return self._idle.wait(timeout)

    def resume(self) -> None:
        self._suspended.clear()
        self._wake.set()

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    # -- loop -----------------------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._suspended.is_set() or self.cells.paused:
                    self._sleep(5)
                    continue
                job = self.store.next_job()
                if job is None:
                    self._sleep(30)
                    continue
                if self.free_bytes() < self.min_free_bytes:
                    self.status.service("disk", "paused", "磁盘剩余空间不足 1 GB，下载已暂停")
                    self.cells.set_paused(True)
                    continue
                self.status.service("disk", "ok")
                self._idle.clear()
                try:
                    if not self._suspended.is_set():
                        self._process(job)
                finally:
                    self._idle.set()
            except overpass.Offline:
                self.status.service("overpass", "paused", "无法连接 Overpass 服务器，下载将在恢复联网后继续")
                self._wait_before_retry(OFFLINE_RETRY, "无法连接任何 Overpass 服务器，稍后自动重试")
            except ServersBusy:
                self.status.service("overpass", "paused", "Overpass 服务器繁忙，稍后自动重试")
                self._wait_before_retry(BUSY_RETRY, "Overpass 服务器都很忙，稍后自动重试")
            except BudgetExhausted:
                self.status.service("overpass", "paused", "今日 Overpass 用量已达上限，将在 UTC 0 点后继续")
                self._wait_before_retry(_seconds_to_utc_midnight(), "今日 Overpass 用量已达上限，UTC 0 点后继续")
            except Exception:
                log.exception("downloader loop error")
                self._sleep(10)

    def _wait_before_retry(self, seconds: float, text: str) -> None:
        """Show the waiting cell why and until when, then sleep."""
        if self._last_job is not None:
            self.cells.set_activity(self._last_job, text, until=time.time() + seconds)
            self._publish(self._last_job, force=True)
        self._sleep(seconds)

    def _activity(self, cid: str, text: str, since: float | None = None, until: float | None = None) -> None:
        self.cells.set_activity(cid, text, since, until)
        self._publish(cid, force=True)

    def _received(self, cid: str, size: int) -> None:
        self.cells.add_received(cid, size)
        self._publish(cid)

    def _process(self, job: dict) -> None:
        cid, x, y = job["id"], job["x"], job["y"]
        updating = job["status"] == "updating"
        with_basemap = self.basemap is not None and (not updating or job["update_kind"] == "all")
        cancel = threading.Event()
        with self._current_lock:
            self._current = (cid, cancel)
        self._last_job = cid
        if updating:
            self.store.update_cell(cid, stage="rail", progress=0.0)
        else:
            self.store.update_cell(cid, status="downloading", stage="rail", progress=0.0, error=None)
        self.cells.set_activity(cid, "准备下载铁路数据（Overpass）")
        self._publish(cid, force=True)
        activity = lambda text, since=None, until=None: self._activity(cid, text, since, until)  # noqa: E731
        received = lambda size: self._received(cid, size)  # noqa: E731
        work = self.home.tmp_dir / cid
        shutil.rmtree(work, ignore_errors=True)
        self.home.ensure_dir(work)
        rail_share = RAIL_SHARE if not with_basemap else 0.35
        basemap_share = 0.0 if not with_basemap else 0.35
        try:
            elements = self.client.fetch_cell(
                x, y, cancel, lambda p: self._progress(cid, "rail", rail_share * p), activity, received
            )
            self.status.service("overpass", "ok")
            basemap_info: dict = {}
            if with_basemap:
                self.cells.set_activity(cid, "下载离线底图（Protomaps）", since=time.time())
                self._progress(cid, "basemap", rail_share, force=True)
                basemap_info = self.basemap.fetch(
                    x, y, work, cancel, lambda p: self._progress(cid, "basemap", rail_share + basemap_share * p),
                    received,
                )
            start = rail_share + basemap_share
            self.cells.set_activity(cid, "处理数据、生成本地瓦片", since=time.time())
            self._progress(cid, "process", start, force=True)
            result = processing.build_cell(
                elements, x, y, work / "rail.sqlite",
                progress=lambda p: self._progress(cid, "process", start + (1 - start) * p),
                cancel=cancel,
                meta={"downloaded_at": time.time()},
            )
            self._install(cid, work, result, basemap_info)
        except (overpass.Cancelled, processing.Cancelled):
            log.info("download of %s cancelled", cid)
            self.cells.clear_live(cid)
        except (overpass.Offline, BudgetExhausted, ServersBusy):
            self._requeue(cid, updating)
            raise
        except Exception as exc:
            log.exception("download of %s failed", cid)
            self.cells.clear_live(cid)
            if self.store.cell(cid) is not None:
                if updating:
                    self.store.update_cell(cid, status="cached", update_kind=None, stage=None, progress=0, error=f"更新失败：{exc}")
                else:
                    self.store.update_cell(cid, status="failed", stage=None, progress=0, error=str(exc))
                self.cells.publish_cells()
        finally:
            with self._current_lock:
                self._current = None
            shutil.rmtree(work, ignore_errors=True)

    def _requeue(self, cid: str, updating: bool) -> None:
        if self.store.cell(cid) is None:
            return
        if updating:
            self.store.update_cell(cid, stage=None, progress=0)
        else:
            self.store.update_cell(cid, status="queued", stage=None, progress=0)
        self.cells.publish_cells()

    def _install(self, cid: str, work: Path, result: processing.CellResult, basemap_info: dict) -> None:
        if self.store.cell(cid) is None:  # deleted meanwhile
            return
        target = self.home.cell_dir(cid)
        self.home.ensure_dir(self.home.cells_dir)
        with self.rail_tiles.exclusive(cid):
            if target.exists():
                for existing in target.iterdir():  # keep files this run did not replace (e.g. the basemap)
                    if not (work / existing.name).exists():
                        _retry(lambda e=existing: e.rename(work / e.name))
                old = target.with_name(cid + ".old")
                shutil.rmtree(old, ignore_errors=True)
                _retry(lambda: target.rename(old))
                _retry(lambda: work.rename(target))
                shutil.rmtree(old, ignore_errors=True)
            else:
                _retry(lambda: work.rename(target))
            for callback in list(self._on_install):
                callback(cid, target)
        now = time.time()
        fields = {
            "status": "cached", "stage": None, "progress": 1.0, "error": None, "update_kind": None,
            "rail_at": now, "rail_bytes": result.size,
        }
        fields.update(basemap_info)
        self.store.update_cell(cid, **fields)
        self.store.replace_stations(cid, result.stations)
        self.cells.clear_live(cid)
        self.rail_tiles.set_version(cid, now)
        self.cells.sync_coverage()
        self.cells.bump_tiles()
        self.cells.publish_cells()
        log.info("cell %s installed: %d lines, %d tiles", cid, result.lines, result.tiles)

    # -- progress ---------------------------------------------------------------------------------

    def _progress(self, cid: str, stage: str, value: float, force: bool = False) -> None:
        """Store and publish progress: at most every 0.5 s, but never skip a step of 5 %."""
        now = time.monotonic()
        value = max(0.0, min(1.0, value))
        if (
            not force
            and now - self._last_progress < PUBLISH_INTERVAL
            and abs(value - self._last_progress_value) < PROGRESS_STEP
        ):
            return
        self._last_progress = now
        self._last_progress_value = value
        self.store.update_cell(cid, stage=stage, progress=value)
        self._publish(cid, force=True)

    def _publish(self, cid: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_publish < PUBLISH_INTERVAL:
            return
        self._last_publish = now
        self.cells.publish_cell(cid)


def _retry(action, attempts: int = 6, delay: float = 0.5):
    """Windows: a just-written file can be locked briefly (e.g. by antivirus)."""
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _seconds_to_utc_midnight() -> float:
    now = time.time()
    return 86400 - (now % 86400) + 60
