"""Base data source interface."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from psychofilm_analyzer.models import InputTitle, SourcePayload
from psychofilm_analyzer.utils.cache import CacheStore
from psychofilm_analyzer.utils.http import HttpClient

logger = logging.getLogger(__name__)


class BaseSource(ABC):
    name: str = "base"

    def __init__(self, http: HttpClient, cache: CacheStore, config: dict | None = None):
        self.http = http
        self.cache = cache
        self.config = config or {}

    def fetch(self, item: InputTitle) -> SourcePayload:
        key = item.cache_key()
        cached = self.cache.get(self.name, key)
        if cached is not None:
            try:
                return SourcePayload(**cached)
            except TypeError:
                # tolerate older cache shapes
                payload = SourcePayload(source=self.name)
                for k, v in cached.items():
                    if hasattr(payload, k):
                        setattr(payload, k, v)
                return payload
        try:
            payload = self._fetch(item)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] fetch failed for %s: %s", self.name, item.title, exc)
            payload = SourcePayload(source=self.name, found=False, error=str(exc))
        self.cache.set(self.name, key, payload.to_dict())
        return payload

    @abstractmethod
    def _fetch(self, item: InputTitle) -> SourcePayload:
        raise NotImplementedError

    def _search_titles(self, item: InputTitle) -> list[str]:
        titles = []
        for t in [
            item.english_title,
            item.title,
            item.russian_title,
        ]:
            if t and t.strip() and t.strip() not in titles:
                titles.append(t.strip())
        return titles
