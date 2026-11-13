# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The held rows window of node_work_ledger_query_effect (OMN-24031).

A rows window read by the work ledger query node is held once so every later
page of the same window is cut from the held copy instead of re-reading the
store. The window's TTL and the number of held windows come from the node's
overlay fields ``rows_window_cache_ttl_seconds`` and
``rows_window_cache_max_windows``, falling back to the contract's
``work_ledger_query.rows_window_cache``. The numbers are never in code.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerParity,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_rows_bound import (
    NODE_NAME,
    OVERLAY_FILE_ENV,
)


class ModelRowsWindowCacheSettings(BaseModel):
    """The held rows window's TTL and capacity; no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ttl_seconds: int = Field(ge=1)
    max_windows: int = Field(ge=1)


class ModelRowsWindowCacheOverlay(BaseModel):
    """The deployment fact of the held rows window; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    rows_window_cache_ttl_seconds: int | None = Field(default=None, ge=1)
    rows_window_cache_max_windows: int | None = Field(default=None, ge=1)


def _contract_rows_window_cache(contract_path: Path) -> ModelRowsWindowCacheSettings:
    """The contract's ``work_ledger_query.rows_window_cache`` (positive ints)."""
    with contract_path.open(encoding="utf-8") as handle:
        contract = yaml.safe_load(handle)
    section = contract.get("work_ledger_query") or {}
    cache = section.get("rows_window_cache")
    if not isinstance(cache, dict):
        raise ValueError("work_ledger_query.rows_window_cache must be a mapping")
    values: dict[str, int] = {}
    for key in ("ttl_seconds", "max_windows"):
        value = cache.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(
                f"work_ledger_query.rows_window_cache.{key} must be a positive int"
            )
        values[key] = value
    return ModelRowsWindowCacheSettings(**values)


def load_rows_window_cache_settings(
    contract_path: Path, environ: Mapping[str, str] | None = None
) -> ModelRowsWindowCacheSettings:
    """The held rows window settings: the overlay's values, else the contract's.

    A missing overlay or a None field keeps the contract value; a present
    value replaces it. An invalid overlay raises; it never silently falls back.
    """
    settings = _contract_rows_window_cache(contract_path)
    try:
        overlay = load_structured_overlay(
            NODE_NAME, ModelRowsWindowCacheOverlay, environ, file_env=OVERLAY_FILE_ENV
        )
    except NodeOverlayNotFoundError:
        return settings
    ttl = overlay.rows_window_cache_ttl_seconds
    max_windows = overlay.rows_window_cache_max_windows
    return ModelRowsWindowCacheSettings(
        ttl_seconds=ttl if ttl is not None else settings.ttl_seconds,
        max_windows=max_windows if max_windows is not None else settings.max_windows,
    )


@dataclass(frozen=True)
class WindowSnapshot:
    """One held rows window: the ordered rows and their newest timestamps."""

    ordered: tuple[ModelWorkLedgerRowRecord, ...]
    rows_read: int
    newest_row_ts: datetime | None
    newest_projected_at: datetime | None
    parity: ModelWorkLedgerParity


class RowsWindowCache:
    """A bounded, thread-safe cache of held rows windows."""

    def __init__(
        self,
        settings: ModelRowsWindowCacheSettings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[object, ...], tuple[WindowSnapshot, float]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple[object, ...]) -> WindowSnapshot | None:
        """The held snapshot for ``key``, or None when absent or expired."""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            snapshot, stamp = entry
            if now - stamp > self._settings.ttl_seconds:
                del self._entries[key]
                self.misses += 1
                return None
            self.hits += 1
            return snapshot

    def put(self, key: tuple[object, ...], snapshot: WindowSnapshot) -> None:
        """Hold ``snapshot`` for ``key``, dropping the oldest entry when full."""
        now = self._clock()
        with self._lock:
            self._entries[key] = (snapshot, now)
            while len(self._entries) > self._settings.max_windows:
                oldest = min(self._entries, key=lambda k: self._entries[k][1])
                del self._entries[oldest]
