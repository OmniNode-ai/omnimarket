# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reader for ``configs/model_bindings.v1.yaml``, the declared model ids of omnimarket code (OMN-21760).

Code names no model id: a node reads its binding here and takes it by injection.
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


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    raw = yaml.safe_load(_BINDINGS_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise KeyError(f"{_BINDINGS_PATH.name} root must be a mapping")
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
