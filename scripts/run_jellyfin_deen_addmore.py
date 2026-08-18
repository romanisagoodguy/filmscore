#!/usr/bin/env python3
"""After the first jellyfin_deen_hdd gather exits, inherit it and fetch the extra 100 films."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Scripts\Grk\hdhdd.ru")
OUT = ROOT / "jellyfin_deen_hdd"
CKPT = OUT / "gather_checkpoint.jsonl"
LIVE = OUT / "profile_a2_live.json"
LATEST = OUT / "profile_a2_latest.json"
UNIFIED = OUT / "gather_v2" / "reports" / "UNIFIED_REPORT.txt"
LOG = OUT / "run_addmore.log"
PYTHON = sys.executable


def _gather_running() -> bool:
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None
    if psutil:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
            except Exception:
                continue
            if "run.py" in cmd and "jellyfin_deen_hdd" in cmd and "gather" in cmd:
                if "run_jellyfin_deen_addmore" in cmd:
                    continue
                return True
        return False
    # Fallback: PowerShell
    r = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*run.py*gather*jellyfin_deen_hdd*' -and "
            "$_.CommandLine -notlike '*run_jellyfin_deen_addmore*' } | "
            "Select-Object -ExpandProperty ProcessId",
        ],
        capture_output=True,
        text=True,
    )
    return bool((r.stdout or "").strip())


def _rows_from_profile_file(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "profiles" in data:
        return list(data["profiles"] or [])
    if isinstance(data, list):
        return data
    return []


def _is_real_profile(p: dict) -> bool:
    err = str(p.get("error") or "")
    if err.startswith("missing from A1"):
        return False
    if p.get("imported"):
        return True
    if p.get("imported_file") or p.get("imported_title"):
        return True
    return bool((p.get("coverage") or {}).get("sources_found"))


def _resume_key_for(p: dict) -> str:
    imp = p.get("imported") or {}
    if imp:
        return "|".join(
            [
                str(imp.get("file") or ""),
                str(imp.get("sheet") or ""),
                str(imp.get("row") or ""),
                str(imp.get("title") or "").strip().lower(),
                str(imp.get("year") if imp.get("year") is not None else p.get("year") or ""),
            ]
        )
    return "|".join(
        [
            str(p.get("imported_file") or ""),
            str(p.get("imported_sheet") or ""),
            str(p.get("imported_row") or ""),
            str(p.get("imported_title") or "").strip().lower(),
            str(p.get("imported_year") if p.get("imported_year") is not None else p.get("year") or ""),
        ]
    )


def _load_profiles() -> list[dict]:
    candidates: list[Path] = []
    if LIVE.exists():
        candidates.append(LIVE)
    stamped = sorted(OUT.glob("profile_a2_20*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    candidates.extend(stamped[:3])
    if LATEST.exists():
        candidates.append(LATEST)
    best: list[dict] = []
    best_src = None
    for path in candidates:
        if not path.exists() or path.stat().st_size < 1000:
            continue
        try:
            rows = [p for p in _rows_from_profile_file(path) if _is_real_profile(p)]
        except Exception as exc:
            print(f"skip {path.name}: {exc}", flush=True)
            continue
        print(f"candidate {path.name}: {len(rows)} real profiles", flush=True)
        if len(rows) > len(best):
            best, best_src = rows, path
    if not best:
        raise SystemExit("No finished profiles found to inherit (profile_a2_live/latest).")
    print(f"using {best_src.name} ({len(best)} profiles)", flush=True)
    return best


def _write_checkpoint(profiles: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with CKPT.open("w", encoding="utf-8") as fh:
        for p in profiles:
            row = dict(p)
            if not row.get("imported"):
                row["imported"] = {
                    "file": row.get("imported_file"),
                    "sheet": row.get("imported_sheet"),
                    "row": row.get("imported_row"),
                    "title": row.get("imported_title"),
                    "year": row.get("imported_year") if row.get("imported_year") is not None else row.get("year"),
                }
            row["_resume_key"] = _resume_key_for(row)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote inherit checkpoint {CKPT} ({len(profiles)} rows)", flush=True)


def main() -> int:
    print("Waiting for first jellyfin_deen_hdd gather to finish...", flush=True)
    waited = 0
    while _gather_running():
        time.sleep(30)
        waited += 30
        if waited % 300 == 0:
            print(f"  still running ({waited // 60} min)", flush=True)
    print("First gather is not running. Snapshot + add-more batch.", flush=True)
    time.sleep(5)
    profiles = _load_profiles()
    _write_checkpoint(profiles)
    cmd = [
        PYTHON,
        "-u",
        str(ROOT / "run.py"),
        "gather",
        "--approach",
        "3",
        "-i",
        str(ROOT / "jellyfin deen" / "films_jellyfin_deen.csv"),
        "-o",
        str(OUT),
        "-c",
        str(ROOT / "config" / "jellyfin_deen_a3_add.yaml"),
        "--no-resume",
    ]
    print("launch:", " ".join(cmd), flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"\n--- add-more gather start ---\n")
        fh.flush()
        proc = subprocess.run(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT)
    print(f"add-more gather exit={proc.returncode}", flush=True)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
