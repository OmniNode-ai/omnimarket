# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reader of the network-binding authority, ``configs/network_bindings.yaml`` (OMN-21623).

Every host, address and port that omnimarket code binds, probes or compares against
is declared in that file and read here by dotted reference. A missing file, a
missing reference or a value of the wrong shape raises ``KeyError`` or
``FileNotFoundError``; there is no default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_AUTHORITY_PATH = (
    Path(__file__).resolve().parent.parent / "configs" / "network_bindings.yaml"
)


@lru_cache(maxsize=1)
def _load_authority() -> dict[str, Any]:
    if not _AUTHORITY_PATH.exists():
        raise FileNotFoundError(f"network_bindings.yaml not found at {_AUTHORITY_PATH}")
    raw = yaml.safe_load(_AUTHORITY_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise KeyError(f"network_bindings.yaml root must be a mapping, got {type(raw)}")
    return raw


def _leaf(reference: str) -> Any:
    obj: Any = _load_authority()
    for key in reference.split("."):
        if not isinstance(obj, dict) or key not in obj:
            raise KeyError(f"network_bindings.yaml missing required key: {reference}")
        obj = obj[key]
    return obj


def network_host(reference: str) -> str:
    """Return the non-empty host or address string declared at the dotted ``reference``."""
    value = _leaf(reference)
    if not isinstance(value, str) or not value.strip():
        raise KeyError(
            f"network_bindings.yaml key {reference!r} must be a non-empty string"
        )
    return value


def network_hosts(reference: str) -> tuple[str, ...]:
    """Return the non-empty list of non-empty host strings declared at ``reference``."""
    value = _leaf(reference)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise KeyError(
            f"network_bindings.yaml key {reference!r} must be a non-empty list of strings"
        )
    return tuple(value)


def network_port(reference: str) -> int:
    """Return the TCP port (1..65535) declared at the dotted ``reference``."""
    value = _leaf(reference)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise KeyError(
            f"network_bindings.yaml key {reference!r} must be a port in 1..65535"
        )
    return value
