"""Background download queue (SPEC §9.1, §9.5, §9.6).

Up to ``WORKERS`` cells at a time, each on its own: railway data (Overpass)
-> offline basemap (Protomaps, if configured) -> processing -> atomic
install.  While one cell waits for an Overpass server, another can download
its basemap or be processed.  How many requests go to each server is limited
there (overpass.py, pmtiles.py), not here.  Processing runs one cell at a
time, which bounds memory and CPU use.

The railway data comes from regional extracts (extracts.py) when many cells
are waiting (``EXTRACT_MIN_CELLS``) or the cell's regions are already on
disk, otherwise from Overpass; if an extract cannot be used, from Overpass.

Work happens in ``data/tmp/<cell>``; the cell only becomes "cached" once
everything is in place, so the page never shows half a cell.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from . import extracts, overpass, processing
from .cells import CellService
from .extracts import EXTRACT_MIN_CELLS, ExtractStore, ExtractUnavailable
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
WORKERS = 3  # cells downloaded at the same time
PROCESS_AT_ONCE = 1  # cells processed at the same time


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
        workers: int = WORKERS,
        extract_store: ExtractStore | None = None,
    ) -> None:
        self.home = home
        self.store = store
        self.cells = cells
        self.client = client
        self.rail_tiles = rail_tiles
        self.status = status
        self.basemap = basemap
        self.workers = workers
        self.extracts = extract_store
        self._plan: dict[str, list[str]] | None = None  # cell -> regions; None: plan again
        self._plan_lock = threading.Lock()
        self._plan_failed_until = 0.0
        self._wake = threading.Condition()
        self._wake_count = 0
        self._stop = threading.Event()
        self._suspended = threading.Event()  # set while the data folder is being cleared
        self._threads: list[threading.Thread] = []
        self.min_free_bytes = 1024**3  # SPEC §9.7: pause below 1 GB free
        self.free_bytes: Callable[[], int] = lambda: shutil.disk_usage(self.home.root).free
        # Cells being worked on -> their cancel event.
        self._jobs: dict[str, threading.Event] = {}
        self._jobs_changed = threading.Condition()
        self._processing = threading.Semaphore(PROCESS_AT_ONCE)
        self._last_publish: dict[str, float] = {}
        self._last_progress: dict[str, tuple[float, float]] = {}
        self._on_install: list[Callable[[str, Path], None]] = []

    def on_install(self, callback: Callable[[str, Path], None]) -> None:
        """Run ``callback(cell_id, cell_dir)`` right after a cell is installed."""
        self._on_install.append(callback)

    # -- control ------------------------------------------------------------------------

    def start(self) -> None:
        for index in range(self.workers):
            thread = threading.Thread(target=self._run, name=f"downloader-{index + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def queue_changed(self) -> None:
        """Cells were added or removed: plan the data sources again, and get going."""
        with self._plan_lock:
            self._plan = None
        self.wake()

    def wake(self) -> None:
        with self._wake:
            self._wake_count += 1
            self._wake.notify_all()

    def cancel(self, ids: list[str]) -> None:
        with self._jobs_changed:
            for cid in ids:
                if cid in self._jobs:
                    self._jobs[cid].set()
        for cid in ids:
            x, y = (int(v) for v in cid.split("-"))
            self.client.forget_cell(x, y)
            self.cells.clear_live(cid)

    def cancel_all(self) -> None:
        with self._jobs_changed:
            for event in self._jobs.values():
                event.set()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        self.cancel_all()
        self.wake()
        end = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, end - time.monotonic()))

    @property
    def busy(self) -> bool:
        return bool(self._jobs)

    def suspend(self, timeout: float = 20.0) -> bool:
        """Stop working (cancel every cell in progress) until ``resume``; True when idle."""
        self._suspended.set()
        self.cancel_all()
        self.wake()
        with self._jobs_changed:
            return self._jobs_changed.wait_for(lambda: not self._jobs, timeout)

    def resume(self) -> None:
        self._suspended.clear()
        self.wake()

    def _sleep(self, seconds: float) -> None:
        """Sleep until ``seconds`` passed or someone called ``wake``."""
        with self._wake:
            count = self._wake_count
            self._wake.wait_for(lambda: self._wake_count != count or self._stop.is_set(), seconds)

    # -- loop -----------------------------------------------------------------------------------

    def _claim(self) -> tuple[dict, threading.Event] | None:
        """The next queued cell nobody is working on yet, or None."""
        with self._jobs_changed:
            if self._suspended.is_set() or self._stop.is_set():
                return None
            job = self.store.next_job(exclude=set(self._jobs))
            if job is None:
                return None
            if self.free_bytes() < self.min_free_bytes:
                self.status.service("disk", "paused", "磁盘剩余空间不足 1 GB，下载已暂停")
                self.cells.set_paused(True)
                return None
            self.status.service("disk", "ok")
            cancel = threading.Event()
            self._jobs[job["id"]] = cancel
            return job, cancel

    def _release(self, cid: str) -> None:
        with self._jobs_changed:
            self._jobs.pop(cid, None)
            self._jobs_changed.notify_all()
        self._last_publish.pop(cid, None)
        self._last_progress.pop(cid, None)

    def _run(self) -> None:
        last_job: str | None = None  # the cell to tell about a wait before the next attempt
        while not self._stop.is_set():
            try:
                if self._suspended.is_set() or self.cells.paused:
                    self._sleep(5)
                    continue
                claimed = self._claim()
                if claimed is None:
                    self._sleep(30)
                    continue
                job, cancel = claimed
                last_job = job["id"]
                try:
                    self._process(job, cancel)
                finally:
                    self._release(job["id"])
            except overpass.Offline:
                self.status.service("overpass", "paused", "无法连接 Overpass 服务器，下载将在恢复联网后继续")
                self._wait_before_retry(last_job, OFFLINE_RETRY, "无法连接任何 Overpass 服务器，稍后自动重试")
            except ServersBusy:
                self.status.service("overpass", "paused", "Overpass 服务器繁忙，稍后自动重试")
                self._wait_before_retry(last_job, BUSY_RETRY, "Overpass 服务器都很忙，稍后自动重试")
            except BudgetExhausted:
                self.status.service("overpass", "paused", "今日 Overpass 用量已达上限，将在 UTC 0 点后继续")
                self._wait_before_retry(
                    last_job, _seconds_to_utc_midnight(), "今日 Overpass 用量已达上限，UTC 0 点后继续"
                )
            except Exception:
                log.exception("downloader loop error")
                self._sleep(10)

    def _wait_before_retry(self, cid: str | None, seconds: float, text: str) -> None:
        """Show the waiting cell why and until when, then sleep."""
        if cid is not None and self.store.cell(cid) is not None:
            self.cells.set_activity(cid, text, until=time.time() + seconds)
            self._publish(cid, force=True)
        self._sleep(seconds)

    def _activity(self, cid: str, text: str, since: float | None = None, until: float | None = None) -> None:
        self.cells.set_activity(cid, text, since, until)
        self._publish(cid, force=True)

    def _received(self, cid: str, size: int) -> None:
        self.cells.add_received(cid, size)
        self._publish(cid)

    def _extract_regions(self, cid: str, x: int, y: int) -> list[str] | None:
        """The regions to read the cell's railway data from, or None for Overpass."""
        if self.extracts is None or time.time() < self._plan_failed_until:
            return None
        with self._plan_lock:
            if self._plan is None:
                pending = self.store.pending_cells()
                try:
                    self._plan = self.extracts.plan(pending) if len(pending) >= EXTRACT_MIN_CELLS else {}
                except Exception as exc:  # noqa: BLE001 - without a plan every cell uses Overpass
                    log.warning("no extract plan: %s", exc)
                    self._plan_failed_until = time.time() + extracts.FAILED_PAUSE
                    self._plan = {}
                if self._plan:
                    log.info("%d of %d waiting cells get their railway data from regional extracts",
                             len(self._plan), len(pending))
            regions = self._plan.get(cid)
            if regions is None and self.extracts.any_fresh():
                # Regions downloaded recently are used even for a few cells.
                try:
                    regions = self.extracts.plan([(x, y)]).get(cid)
                except Exception:  # noqa: BLE001 - Overpass then
                    regions = None
                if regions and not all(self.extracts.fresh(r) for r in regions):
                    regions = None
        if regions and all(self.extracts.usable(r) for r in regions):
            return regions
        return None

    def _rail_data(self, cid: str, x: int, y: int, cancel: threading.Event, rail_share: float, activity, received):
        regions = self._extract_regions(cid, x, y)
        if regions:
            try:
                elements = self.extracts.cell_elements(
                    x, y, regions, cancel, activity, received,
                    lambda f: self._progress(cid, "rail", rail_share * 0.9 * f),
                )
                return elements
            except extracts.Cancelled:
                raise overpass.Cancelled() from None
            except ExtractUnavailable as exc:
                # Shown in the cell's line of the download panel; the status bar is about the network.
                log.warning("cell %s: %s; using Overpass", cid, exc)
                self._activity(cid, "地区数据包不可用，改用 Overpass")
        elements = self.client.fetch_cell(
            x, y, cancel, lambda p: self._progress(cid, "rail", rail_share * p), activity, received
        )
        self.status.service("overpass", "ok")
        return elements

    def _process(self, job: dict, cancel: threading.Event) -> None:
        cid, x, y = job["id"], job["x"], job["y"]
        updating = job["status"] == "updating"
        with_basemap = self.basemap is not None and (not updating or job["update_kind"] == "all")
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
            elements = self._rail_data(cid, x, y, cancel, rail_share, activity, received)
            basemap_info: dict = {}
            if with_basemap:
                self.cells.set_activity(cid, "下载离线底图（Protomaps）", since=time.time())
                self._progress(cid, "basemap", rail_share, force=True)
                basemap_info = self.basemap.fetch(
                    x, y, work, cancel, lambda p: self._progress(cid, "basemap", rail_share + basemap_share * p),
                    received,
                )
            start = rail_share + basemap_share
            self._progress(cid, "process", start, force=True)
            self._activity(cid, "等待处理（另一格正在处理）", since=time.time())
            while not self._processing.acquire(timeout=0.5):
                if cancel.is_set():
                    raise processing.Cancelled()
            try:
                self._activity(cid, "处理数据、生成本地瓦片", since=time.time())
                result = processing.build_cell(
                    elements, x, y, work / "rail.sqlite",
                    progress=lambda p: self._progress(cid, "process", start + (1 - start) * p),
                    cancel=cancel,
                    meta={"downloaded_at": time.time()},
                )
                del elements  # the largest thing in memory; not needed any more
                self._install(cid, work, result, basemap_info)
            finally:
                self._processing.release()
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
        """Store and publish a cell's progress: at most every 0.5 s, but never skip a step of 5 %."""
        now = time.monotonic()
        value = max(0.0, min(1.0, value))
        last_time, last_value = self._last_progress.get(cid, (0.0, -1.0))
        if not force and now - last_time < PUBLISH_INTERVAL and abs(value - last_value) < PROGRESS_STEP:
            return
        self._last_progress[cid] = (now, value)
        self.store.update_cell(cid, stage=stage, progress=value)
        self._publish(cid, force=True)

    def _publish(self, cid: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_publish.get(cid, 0.0) < PUBLISH_INTERVAL:
            return
        self._last_publish[cid] = now
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
