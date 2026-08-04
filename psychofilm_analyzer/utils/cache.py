"""Disk / SQLite-backed cache for external API responses."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class CacheStore:
    def __init__(self, cache_dir: str | Path = "cache", ttl_days: int = 30, enabled: bool = True):
        self.enabled = enabled
        self.ttl_sec = max(1, ttl_days) * 86400
        self.root = Path(cache_dir)
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)
        self._memory: dict[str, Any] = {}

    def _path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(f"{namespace}:{key}".encode("utf-8")).hexdigest()
        folder = self.root / namespace
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{digest}.json"

    def get(self, namespace: str, key: str) -> Optional[Any]:
        if not self.enabled:
            return None
        mem_key = f"{namespace}:{key}"
        if mem_key in self._memory:
            return self._memory[mem_key]
        path = self._path(namespace, key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - data.get("_ts", 0) > self.ttl_sec:
                return None
            value = data.get("value")
            self._memory[mem_key] = value
            return value
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cache read failed %s: %s", path, exc)
            return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        mem_key = f"{namespace}:{key}"
        self._memory[mem_key] = value
        path = self._path(namespace, key)
        try:
            path.write_text(
                json.dumps({"_ts": time.time(), "value": value}, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cache write failed %s: %s", path, exc)

    def clear_namespace(self, namespace: str) -> int:
        folder = self.root / namespace
        if not folder.exists():
            return 0
        n = 0
        for p in folder.glob("*.json"):
            p.unlink(missing_ok=True)
            n += 1
        return n
