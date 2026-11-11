# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Memoise load_task_class_authority for the R4 scenario tests (OMN-23695).

Each R4 scenario run calls ``omnimarket.inference.task_class_authority.load_task_class_authority``
about nine times per test.  The function parses a large YAML file with the pure-python loader,
which costs roughly 0.4 s per call, so ten tests cost about 25 s.  This conftest memoises the
result for the tests in this directory only, with no change to production code.

The cache key is ``(str(resolved path) or None, st_mtime_ns or None)`` so that a changed file
invalidates the cached value.  The original function is captured once at import time so the
wrapper never calls itself.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers import execution_budget_overlay
from omnimarket.inference import task_class_authority

# Capture the original function once at import time, before any patching.
_REAL_LOAD: Callable[..., Any] = task_class_authority.load_task_class_authority

# Module-level cache: (str(resolved path) or None, st_mtime_ns or None) -> result
_CACHE: dict[tuple[str | None, int | None], Any] = {}


def _cache_key(config_path: str | Path | None) -> tuple[str | None, int | None]:
    """Build the cache key from the resolved path and its mtime."""
    if config_path is None:
        return (None, None)
    resolved = Path(config_path).resolve()
    key_path = str(resolved)
    try:
        mtime_ns: int | None = resolved.stat().st_mtime_ns
    except OSError:
        mtime_ns = None
    return (key_path, mtime_ns)


def _memoised_load(config_path: str | Path | None = None) -> Any:
    """Wrapper that caches the result of the real load_task_class_authority."""
    key = _cache_key(config_path)
    if key in _CACHE:
        return _CACHE[key]
    result = _REAL_LOAD(config_path)
    _CACHE[key] = result
    return result


@pytest.fixture(autouse=True)
def _memoise_task_class_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch load_task_class_authority on both modules for the duration of each test."""
    monkeypatch.setattr(
        task_class_authority, "load_task_class_authority", _memoised_load
    )
    monkeypatch.setattr(
        execution_budget_overlay, "load_task_class_authority", _memoised_load
    )
