# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reader of the runtime-path authority, ``configs/runtime_paths.yaml`` (OMN-21623).

Every filesystem location a node falls back to is declared in that file and read
here by dotted reference. A missing file, a missing reference or a value of the
wrong shape raises ``KeyError`` or ``FileNotFoundError``; there is no default.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_AUTHORITY_PATH = (
    Path(__file__).resolve().parent.parent / "configs" / "runtime_paths.yaml"
)


def _read_authority(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"runtime_paths.yaml not found at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise KeyError(f"runtime_paths.yaml root must be a mapping, got {type(raw)}")
    return raw


@lru_cache(maxsize=1)
def _load_authority() -> dict[str, Any]:
    return _read_authority(_AUTHORITY_PATH)


def _leaf(reference: str) -> Any:
    obj: Any = _load_authority()
    for key in reference.split("."):
        if not isinstance(obj, dict) or key not in obj:
            raise KeyError(f"runtime_paths.yaml missing required key: {reference}")
        obj = obj[key]
    return obj


def runtime_path(reference: str) -> str:
    """Return the non-empty string declared at the dotted ``reference``."""
    value = _leaf(reference)
    if not isinstance(value, str) or not value.strip():
        raise KeyError(
            f"runtime_paths.yaml key {reference!r} must be a non-empty string"
        )
    return value


def runtime_path_list(reference: str) -> tuple[str, ...]:
    """Return the non-empty list of non-empty strings declared at ``reference``."""
    value = _leaf(reference)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise KeyError(
            f"runtime_paths.yaml key {reference!r} must be a non-empty list of strings"
        )
    return tuple(value)


def runtime_dir(reference: str, env_key: str | None = None) -> str:
    """Return the location at ``reference`` with ``~`` expanded; ``env_key``, when set, wins."""
    declared = runtime_path(reference)
    return os.path.expanduser(
        os.environ.get(env_key, declared) if env_key else declared
    )


def runtime_location(reference: str, env_key: str, xdg_parts: tuple[str, ...]) -> str:
    """Resolve a location from ``env_key``, then ``$XDG_RUNTIME_DIR/<xdg_parts>``, then ``reference``."""
    env_path = os.environ.get(env_key)
    if env_path:
        return env_path
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, *xdg_parts)
    return runtime_path(reference)


__all__ = ["runtime_dir", "runtime_location", "runtime_path", "runtime_path_list"]
