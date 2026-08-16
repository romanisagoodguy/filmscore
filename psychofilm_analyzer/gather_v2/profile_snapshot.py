"""Periodic profile_a2 Excel/JSON snapshots during Approach 2 gather."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from psychofilm_analyzer.utils.localtime import now_str

logger = logging.getLogger(__name__)


def _now() -> str:
    return now_str()


class ProfileSnapshotWriter:
    """
    Background thread: every interval_sec assemble current A2 profiles,
    merge with A1, and write profile_a2_*.xlsx + profile_a2_live.xlsx to disk.
    """

    def __init__(
        self,
        *,
        assemble_fn: Callable[[], list[dict[str, Any]]],
        output_dir: str | Path = "output",
        interval_sec: float = 1200.0,
        prefix: str = "profile_a2",
        write_excel: bool = True,
        log_path: Optional[str | Path] = None,
    ):
        self.assemble_fn = assemble_fn
        self.output_dir = Path(output_dir)
        self.interval_sec = max(30.0, float(interval_sec))
        self.prefix = prefix
        self.write_excel = write_excel
        self.log_path = Path(log_path) if log_path else None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.last_paths: dict[str, Path] = {}
        self.last_error: str = ""
        self.snapshot_count = 0

    def _log(self, msg: str) -> None:
        line = f"[{_now()}] {msg}"
        logger.info("%s", msg)
        print(f"  profile snapshot: {msg}", flush=True)
        if self.log_path:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass

    def write_once(self, *, reason: str = "interval") -> dict[str, Path]:
        """Assemble + write current profiles (thread-safe)."""
        from psychofilm_analyzer.enrichment.export import write_profile_dicts

        with self._lock:
            t0 = time.monotonic()
            try:
                profiles = self.assemble_fn() or []
                written = write_profile_dicts(
                    profiles,
                    output_dir=self.output_dir,
                    prefix=self.prefix,
                    write_excel=self.write_excel and len(profiles) <= 20000,
                )
                # Stable live paths for easy open during long runs
                if self.write_excel and "excel" in written:
                    live_xlsx = self.output_dir / f"{self.prefix}_live.xlsx"
                    try:
                        import shutil

                        shutil.copy2(written["excel"], live_xlsx)
                        written["live_excel"] = live_xlsx
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("live excel copy failed: %s", exc)
                if "json" in written:
                    live_json = self.output_dir / f"{self.prefix}_live.json"
                    try:
                        import shutil

                        shutil.copy2(written["json"], live_json)
                        written["live_json"] = live_json
                    except Exception:
                        pass

                self.last_paths = dict(written)
                self.snapshot_count += 1
                elapsed = time.monotonic() - t0
                paths_s = ", ".join(f"{k}={v}" for k, v in written.items())
                self._log(
                    f"{reason}: n={len(profiles)} in {elapsed:.1f}s  {paths_s}"
                )
                self.last_error = ""
                return written
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self._log(f"{reason} FAILED: {exc}")
                logger.exception("profile snapshot failed")
                return {}

    def start(self, *, write_immediately: bool = True) -> None:
        if write_immediately:
            # Delay first snapshot so pipelines are not starved by Excel on huge catalogs
            def _first() -> None:
                if not self._stop.wait(180.0):
                    self.write_once(reason="startup")

            threading.Thread(target=_first, name="a2-profile-snap-first", daemon=True).start()

        self._thread = threading.Thread(
            target=self._loop, name="a2-profile-snapshot", daemon=True
        )
        self._thread.start()
        self._log(
            f"started interval={self.interval_sec:.0f}s "
            f"({self.interval_sec / 60.0:.1f} min) → {self.output_dir / (self.prefix + '_*.xlsx')}"
        )

    def stop(self, *, final_write: bool = True) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=min(30.0, self.interval_sec + 5))
        if final_write:
            self.write_once(reason="final")
        self._log(f"stopped after {self.snapshot_count} snapshot(s)")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            self.write_once(reason="interval")
