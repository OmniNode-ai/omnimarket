# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reader for ``configs/model_bindings.v1.yaml``, the declared model ids of omnimarket code (OMN-21760).

Code names no model id: a node reads its binding here and takes it by injection.
The pricing and brief bindings (OMN-21805) sit in the contract overlay tree,
``config/onex/*/pricing_model_bindings.v1.yaml`` (the contract overlay tree), and are merged over the packaged file
(OMN-26651): the wheel ships that overlay beside the packaged file, and a source checkout reads it
from the repository root.
The reader fails closed -- a missing section, key or empty value raises
``KeyError`` at the first read, with no silent fallback.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_BINDINGS_PATH = (
    Path(__file__).resolve().parent.parent / "configs" / "model_bindings.v1.yaml"
)


_OVERLAY_NAME = "pricing_model_bindings.v1.yaml"
# The wheel ships the config/onex tree as onex_config beside the package; a source checkout reads it
# from the repository root. The tree's own subdirectory is matched by glob, never spelled here.
_OVERLAY_GLOBS = (
    (_BINDINGS_PATH.parent.parent, f"onex_config/*/{_OVERLAY_NAME}"),
    (Path(__file__).resolve().parents[3], f"config/onex/*/{_OVERLAY_NAME}"),
)


def _read_mapping(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise KeyError(f"{path.name} root must be a mapping")
    return raw


def _overlay_path() -> Path:
    for root, pattern in _OVERLAY_GLOBS:
        for candidate in sorted(root.glob(pattern)):
            if candidate.is_file():
                return candidate
    raise KeyError(
        f"{_OVERLAY_NAME} not found in the packaged or checkout config/onex tree"
    )


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    raw = _read_mapping(_BINDINGS_PATH)
    overlay = _read_mapping(_overlay_path())
    for section, value in overlay.items():
        current = raw.get(section)
        if isinstance(current, dict) and isinstance(value, dict):
            raw[section] = {**current, **value}
        else:
            raw[section] = value
    return raw


def model_binding_section(section: str) -> Any:
    """Return a copy of one top-level section of the bindings file, failing closed when absent."""
    document = _load()
    if section not in document:
        raise KeyError(f"{_BINDINGS_PATH.name} missing required section: {section}")
    return deepcopy(document[section])


def model_binding(name: str) -> str:
    """Return the non-empty string bound to ``name`` in the ``bindings`` section."""
    bindings = model_binding_section("bindings")
    value = bindings.get(name) if isinstance(bindings, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise KeyError(
            f"{_BINDINGS_PATH.name} bindings.{name} must be a non-empty string"
        )
    return value


__all__ = ["model_binding", "model_binding_section"]
