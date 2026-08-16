"""Adaptive RPM controller for Approach 2 (especially Wikipedia).

Policy (user-specified):
  - Raise RPM **stepwise** (absolute steps) until max_rpm (default 200).
  - Remember **stable_rpm** = last rate that completed a full success batch.
  - On HTTP 429:
      * cool-down pause (base + step growth)
      * **roll back RPM to stable_rpm** (last known good)
      * if already at stable, step down one more step and lower stable
  - After every success_batch successes at current rate:
      * lock stable_rpm = current
      * try current + step_rpm (capped at max_rpm)
  - Every export / log line shows CURRENT_RPM + STABLE_RPM.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from psychofilm_analyzer.utils.localtime import now_str


def _now() -> str:
    return now_str(with_ms=True)


@dataclass
class AdaptiveRpmState:
    site: str
    rpm: float
    delay_sec: float
    stable_rpm: float
    cool_pause_sec: float
    next_cool_pause_sec: float
    success_streak: int = 0
    total_success: int = 0
    total_429: int = 0
    total_fail_other: int = 0
    total_requests: int = 0
    min_rpm: float = 1.0
    max_rpm: float = 200.0
    step_rpm: float = 10.0
    peak_rpm: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)


class AdaptiveRpmController:
    """Thread-safe adaptive RPM for one pipeline (wikipedia)."""

    def __init__(
        self,
        site: str,
        *,
        initial_rpm: float = 20.0,
        min_rpm: float = 5.0,
        max_rpm: float = 200.0,
        step_rpm: float = 10.0,
        cool_base_sec: float = 30.0,
        cool_step_sec: float = 15.0,
        decrease_pct: float = 0.20,
        increase_pct: float = 0.0,
        success_batch: int = 8,
        log_path: Optional[str | Path] = None,
        live_rpm_path: Optional[str | Path] = None,
    ):
        self.site = site
        self.min_rpm = float(min_rpm)
        self.max_rpm = float(max_rpm)
        self.step_rpm = max(0.5, float(step_rpm))
        self.cool_base_sec = float(cool_base_sec)
        self.cool_step_sec = float(cool_step_sec)
        self.decrease_pct = float(decrease_pct)
        # increase_pct kept for config compat; stepwise uses step_rpm when > 0
        self.increase_pct = float(increase_pct)
        self.success_batch = max(1, int(success_batch))
        self.log_path = Path(log_path) if log_path else None
        self.live_rpm_path = Path(live_rpm_path) if live_rpm_path else None

        rpm = self._clamp_rpm(initial_rpm)
        self._lock = threading.RLock()
        self._state = AdaptiveRpmState(
            site=site,
            rpm=rpm,
            delay_sec=self._rpm_to_delay(rpm),
            stable_rpm=rpm,
            cool_pause_sec=0.0,
            next_cool_pause_sec=self.cool_base_sec,
            min_rpm=self.min_rpm,
            max_rpm=self.max_rpm,
            step_rpm=self.step_rpm,
            peak_rpm=rpm,
        )
        self._last_request_mono = 0.0
        self._write_header()
        self._write_live_rpm_file()
        self._log_event(
            kind="init",
            reason=(
                f"Adaptive RPM started for {site}: "
                f"CURRENT_RPM={rpm:.2f}  STABLE_RPM={rpm:.2f}  "
                f"delay={self._state.delay_sec:.3f}s  "
                f"target_max={self.max_rpm:.0f}  step=+{self.step_rpm:.1f}/batch. "
                f"On 429: GLOBAL cool (base {self.cool_base_sec:.0f}s, "
                f"+{self.cool_step_sec:.0f}s each) and ROLL BACK to STABLE_RPM. "
                f"After every {self.success_batch} OK: lock stable, then raise by "
                f"+{self.step_rpm:.1f} RPM (cap {self.max_rpm:.0f})."
            ),
        )

    @staticmethod
    def _rpm_to_delay(rpm: float) -> float:
        rpm = max(rpm, 0.05)
        return 60.0 / rpm

    @staticmethod
    def delay_to_rpm(delay_sec: float) -> float:
        d = max(float(delay_sec), 0.05)
        return 60.0 / d

    def _clamp_rpm(self, rpm: float) -> float:
        return max(self.min_rpm, min(self.max_rpm, float(rpm)))

    def _format_rpm_banner(self) -> str:
        s = self._state
        return (
            f"CURRENT_RPM={s.rpm:.2f}  STABLE_RPM={s.stable_rpm:.2f}  "
            f"PEAK_RPM={s.peak_rpm:.2f}  DELAY_SEC={s.delay_sec:.3f}  "
            f"BOUNDS=[{s.min_rpm:.1f}..{s.max_rpm:.1f}]  STEP={s.step_rpm:.1f}  "
            f"STREAK={s.success_streak}/{self.success_batch}  "
            f"OK={s.total_success}  429={s.total_429}"
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = self._state
            return {
                "site": s.site,
                "rpm": round(s.rpm, 4),
                "current_rpm": round(s.rpm, 4),
                "stable_rpm": round(s.stable_rpm, 4),
                "peak_rpm": round(s.peak_rpm, 4),
                "delay_sec": round(s.delay_sec, 3),
                "next_cool_pause_sec": round(s.next_cool_pause_sec, 1),
                "last_cool_pause_sec": round(s.cool_pause_sec, 1),
                "success_streak": s.success_streak,
                "success_batch": self.success_batch,
                "total_success": s.total_success,
                "total_429": s.total_429,
                "total_fail_other": s.total_fail_other,
                "total_requests": s.total_requests,
                "min_rpm": s.min_rpm,
                "max_rpm": s.max_rpm,
                "step_rpm": s.step_rpm,
                "events_n": len(s.events),
                "banner": self._format_rpm_banner(),
            }

    def current_delay_sec(self) -> float:
        with self._lock:
            return self._state.delay_sec

    def current_rpm(self) -> float:
        with self._lock:
            return self._state.rpm

    def stable_rpm(self) -> float:
        with self._lock:
            return self._state.stable_rpm

    def wait_turn(self, stop_event: Optional[threading.Event] = None) -> None:
        """Reserve the next global slot (multi-worker safe) then sleep if needed.

        Token-bucket style: each caller advances ``_last_request_mono`` by
        ``delay_sec`` under the lock so N workers share one target RPM instead
        of all racing past the same gap.
        """
        with self._lock:
            delay = max(0.05, float(self._state.delay_sec))
            now = time.monotonic()
            last = self._last_request_mono
            if last <= 0:
                # first request — start immediately, stamp slot
                self._last_request_mono = now
                remain = 0.0
            else:
                next_slot = last + delay
                if next_slot <= now:
                    self._last_request_mono = now
                    remain = 0.0
                else:
                    self._last_request_mono = next_slot
                    remain = next_slot - now
        if remain <= 0:
            return
        if stop_event is not None:
            stop_event.wait(remain)
        else:
            time.sleep(remain)

    def mark_request_started(self) -> None:
        """Count a started request. Slot timing is owned by wait_turn()."""
        with self._lock:
            self._state.total_requests += 1
            # If wait_turn was skipped (non-adaptive path misuse), keep last fresh
            if self._last_request_mono <= 0:
                self._last_request_mono = time.monotonic()
            self._write_live_rpm_file_unlocked()

    def on_success(self, request_id: str = "") -> dict[str, Any]:
        """Record success; every success_batch, lock stable and step RPM up."""
        with self._lock:
            s = self._state
            s.total_success += 1
            s.success_streak += 1
            event: Optional[dict[str, Any]] = None
            if s.success_streak >= self.success_batch:
                old_rpm = s.rpm
                old_delay = s.delay_sec
                # This rate survived a full batch → it is stable
                s.stable_rpm = old_rpm
                # Stepwise climb toward max
                if self.step_rpm > 0:
                    new_rpm = self._clamp_rpm(old_rpm + self.step_rpm)
                elif self.increase_pct > 0:
                    new_rpm = self._clamp_rpm(old_rpm * (1.0 + self.increase_pct))
                else:
                    new_rpm = self._clamp_rpm(old_rpm + 10.0)
                s.rpm = new_rpm
                s.delay_sec = self._rpm_to_delay(new_rpm)
                s.peak_rpm = max(s.peak_rpm, new_rpm)
                s.success_streak = 0
                # Successful stretch: gently decay cool-down back toward base
                if s.next_cool_pause_sec > self.cool_base_sec:
                    s.next_cool_pause_sec = max(
                        self.cool_base_sec,
                        s.next_cool_pause_sec - self.cool_step_sec,
                    )
                at_cap = abs(new_rpm - self.max_rpm) < 1e-6
                reason = (
                    f"SUCCESS_BATCH: {self.success_batch} OK in a row "
                    f"(last={request_id or 'n/a'}). "
                    f"LOCKED STABLE_RPM={s.stable_rpm:.2f}. "
                    f"STEP UP: CURRENT_RPM {old_rpm:.2f} → {s.rpm:.2f} "
                    f"(+{self.step_rpm:.1f}, max={self.max_rpm:.0f}"
                    f"{', AT_CAP' if at_cap else ''}), "
                    f"delay {old_delay:.3f}s → {s.delay_sec:.3f}s. "
                    f"{self._format_rpm_banner()}"
                )
                event = self._log_event_unlocked(
                    kind="rpm_up",
                    reason=reason,
                    extra={
                        "old_rpm": old_rpm,
                        "new_rpm": s.rpm,
                        "stable_rpm": s.stable_rpm,
                        "old_delay_sec": old_delay,
                        "new_delay_sec": s.delay_sec,
                        "request_id": request_id,
                    },
                )
                self._write_live_rpm_file_unlocked()
            else:
                self._write_live_rpm_file_unlocked()
            return event or {
                "kind": "success",
                "rpm": s.rpm,
                "current_rpm": s.rpm,
                "stable_rpm": s.stable_rpm,
                "delay_sec": s.delay_sec,
                "success_streak": s.success_streak,
            }

    def on_429(self, request_id: str = "", repro_cmd: str = "") -> dict[str, Any]:
        """
        On rate limit:
          - cool-down pause
          - ROLL BACK CURRENT_RPM → STABLE_RPM (last known good)
          - if already at stable, step down further and lower stable
        """
        with self._lock:
            s = self._state
            s.total_429 += 1
            s.success_streak = 0
            old_rpm = s.rpm
            old_stable = s.stable_rpm
            old_delay = s.delay_sec
            cool = s.next_cool_pause_sec
            s.cool_pause_sec = cool
            s.next_cool_pause_sec = cool + self.cool_step_sec

            # Roll back to stable if we had climbed above it
            if old_rpm > old_stable + 0.05:
                new_rpm = self._clamp_rpm(old_stable)
                action = (
                    f"ROLL BACK CURRENT_RPM {old_rpm:.2f} → STABLE_RPM {new_rpm:.2f}"
                )
            else:
                # Already at (or below) stable — step down and re-lock stable lower
                if self.step_rpm > 0:
                    new_rpm = self._clamp_rpm(old_rpm - self.step_rpm)
                else:
                    new_rpm = self._clamp_rpm(old_rpm * (1.0 - self.decrease_pct))
                s.stable_rpm = new_rpm
                action = (
                    f"ALREADY_AT_STABLE: step DOWN CURRENT_RPM {old_rpm:.2f} → "
                    f"{new_rpm:.2f}; STABLE_RPM lowered to {s.stable_rpm:.2f}"
                )

            s.rpm = new_rpm
            s.delay_sec = self._rpm_to_delay(new_rpm)
            reason = (
                f"HTTP_429: rate limited on request {request_id or 'n/a'}. "
                f"Action 1) DEFER this request. "
                f"Action 2) GLOBAL pause ALL {self.site} calls = {cool:.0f}s "
                f"(next cool will be {s.next_cool_pause_sec:.0f}s). "
                f"Action 3) {action}. "
                f"delay {old_delay:.3f}s → {s.delay_sec:.3f}s. "
                f"{self._format_rpm_banner()}"
            )
            if repro_cmd:
                reason += f"\nFULL_COMMAND_LINE:\n  {repro_cmd}"
            event = self._log_event_unlocked(
                kind="http_429",
                reason=reason,
                extra={
                    "old_rpm": old_rpm,
                    "new_rpm": s.rpm,
                    "old_stable_rpm": old_stable,
                    "stable_rpm": s.stable_rpm,
                    "old_delay_sec": old_delay,
                    "new_delay_sec": s.delay_sec,
                    "cool_pause_sec": cool,
                    "next_cool_pause_sec": s.next_cool_pause_sec,
                    "request_id": request_id,
                    "total_429": s.total_429,
                    "reproducible_command": repro_cmd,
                },
            )
            self._write_live_rpm_file_unlocked()
            return event

    def on_other_result(
        self,
        *,
        ok: bool,
        request_id: str = "",
        http_status: Any = None,
        break_streak: Optional[bool] = None,
    ) -> None:
        """Non-success / non-429 outcomes.

        By default a failed/other result breaks the success streak. Callers that
        hit a *healthy* terminal outcome (e.g. durable wiki not_found) should
        pass ``break_streak=False`` or use :meth:`on_success` so RPM can climb.
        """
        with self._lock:
            if not ok:
                self._state.total_fail_other += 1
                do_break = True if break_streak is None else bool(break_streak)
                if do_break:
                    self._state.success_streak = 0
            self._write_live_rpm_file_unlocked()

    def apply_cool_pause(self, seconds: float, stop_event: Optional[threading.Event] = None) -> None:
        if seconds <= 0:
            return
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            time.sleep(seconds)

    def _write_header(self) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(
            "ADAPTIVE RPM LOG — Wikipedia / site pipeline\n"
            f"site: {self.site}\n"
            f"started: {_now()}\n"
            f"rules:\n"
            f"  - raise CURRENT_RPM by +{self.step_rpm:.1f} after every "
            f"{self.success_batch} successes (cap max_rpm={self.max_rpm:.0f})\n"
            f"  - lock STABLE_RPM at the rate that completed the batch\n"
            f"  - on 429: cool-down base {self.cool_base_sec:.0f}s "
            f"(+{self.cool_step_sec:.0f}s each); ROLL BACK to STABLE_RPM\n"
            f"  - if already at stable on 429: step DOWN by {self.step_rpm:.1f}\n"
            f"  - every line below includes CURRENT_RPM + STABLE_RPM\n"
            + "=" * 72
            + "\n",
            encoding="utf-8",
        )

    def _write_live_rpm_file(self) -> None:
        with self._lock:
            self._write_live_rpm_file_unlocked()

    def _write_live_rpm_file_unlocked(self) -> None:
        """Tiny always-current file for quick glance + report readers."""
        if not self.live_rpm_path:
            return
        s = self._state
        self.live_rpm_path.parent.mkdir(parents=True, exist_ok=True)
        text = (
            f"site={self.site}\n"
            f"updated={_now()}\n"
            f"CURRENT_RPM={s.rpm:.2f}\n"
            f"STABLE_RPM={s.stable_rpm:.2f}\n"
            f"PEAK_RPM={s.peak_rpm:.2f}\n"
            f"DELAY_SEC={s.delay_sec:.3f}\n"
            f"MIN_RPM={s.min_rpm:.1f}\n"
            f"MAX_RPM={s.max_rpm:.1f}\n"
            f"STEP_RPM={s.step_rpm:.1f}\n"
            f"SUCCESS_STREAK={s.success_streak}/{self.success_batch}\n"
            f"TOTAL_OK={s.total_success}\n"
            f"TOTAL_429={s.total_429}\n"
            f"NEXT_COOL_SEC={s.next_cool_pause_sec:.0f}\n"
            f"LAST_COOL_SEC={s.cool_pause_sec:.0f}\n"
            f"BANNER={self._format_rpm_banner()}\n"
        )
        self.live_rpm_path.write_text(text, encoding="utf-8")

    def _log_event(
        self,
        *,
        kind: str,
        reason: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            return self._log_event_unlocked(kind=kind, reason=reason, extra=extra)

    def _log_event_unlocked(
        self,
        *,
        kind: str,
        reason: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        s = self._state
        event = {
            "time": _now(),
            "time_utc": _now(),  # legacy key name; value is local system time
            "kind": kind,
            "site": self.site,
            "rpm": round(s.rpm, 4),
            "current_rpm": round(s.rpm, 4),
            "stable_rpm": round(s.stable_rpm, 4),
            "peak_rpm": round(s.peak_rpm, 4),
            "delay_sec": round(s.delay_sec, 3),
            "cool_pause_sec": round(s.cool_pause_sec, 1),
            "next_cool_pause_sec": round(s.next_cool_pause_sec, 1),
            "success_streak": s.success_streak,
            "total_success": s.total_success,
            "total_429": s.total_429,
            "reason": reason,
        }
        if extra:
            event.update(extra)
        s.events.append(event)
        if self.log_path:
            block = (
                f"\n--- {_now()}  [{kind}] ---\n"
                f"{self._format_rpm_banner()}\n"
                f"cool_now={event['cool_pause_sec']}  cool_next={event['next_cool_pause_sec']}\n"
                f"REASON: {reason}\n"
            )
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(block)
        return event

    def recent_events_text(self, limit: int = 30) -> str:
        with self._lock:
            ev = self._state.events[-limit:]
        lines = []
        for e in ev:
            lines.append(
                f"[{e.get('time') or e.get('time_utc')}] {e.get('kind')}: "
                f"CURRENT_RPM={e.get('current_rpm', e.get('rpm'))} "
                f"STABLE_RPM={e.get('stable_rpm')} "
                f"delay={e.get('delay_sec')}s cool_next={e.get('next_cool_pause_sec')}s"
            )
            lines.append(f"  {e.get('reason')}")
        return "\n".join(lines)
