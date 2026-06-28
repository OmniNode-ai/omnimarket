# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Service endpoint authority for external API base URLs (OMN-12806).

All GitHub and Linear API base URLs used across omnimarket are resolved from
``configs/service_endpoints.yaml``.  This module loads that YAML once at
import time and exposes typed constants.  Any missing required key raises
``KeyError`` immediately — fail-closed, no silent fallback.

Usage::

    from omnimarket.config.service_endpoints import (
        GITHUB_REST_URL,
        GITHUB_GRAPHQL_URL,
        LINEAR_GRAPHQL_URL,
    )
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "configs" / "service_endpoints.yaml"
)


def _load_config(path: Path = _CONFIG_PATH) -> dict[str, Any]:
    """Load the service endpoint YAML.  Raises FileNotFoundError or KeyError on any gap."""
    if not path.exists():
        raise FileNotFoundError(f"service_endpoints.yaml not found at {path}")
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise KeyError(
            f"service_endpoints.yaml root must be a mapping, got {type(raw)}"
        )
    return raw


def _require(mapping: dict[str, Any], *keys: str) -> str:
    """Walk nested keys and return the leaf string value.  Raises KeyError if absent."""
    obj: Any = mapping
    for key in keys:
        if not isinstance(obj, dict) or key not in obj:
            raise KeyError(
                f"service_endpoints.yaml missing required key: {'.'.join(keys)}"
            )
        obj = obj[key]
    if not isinstance(obj, str) or not obj.strip():
        raise KeyError(
            f"service_endpoints.yaml key {'.'.join(keys)!r} must be a non-empty string"
        )
    return obj


_cfg = _load_config()

#: GitHub REST API base URL — e.g. ``https://api.github.com``
GITHUB_REST_URL: str = _require(_cfg, "github", "rest_url")

#: GitHub GraphQL endpoint — e.g. ``https://api.github.com/graphql``
GITHUB_GRAPHQL_URL: str = _require(_cfg, "github", "graphql_url")

#: Linear GraphQL endpoint — e.g. ``https://api.linear.app/graphql``
LINEAR_GRAPHQL_URL: str = _require(_cfg, "linear", "graphql_url")

__all__ = ["GITHUB_GRAPHQL_URL", "GITHUB_REST_URL", "LINEAR_GRAPHQL_URL"]
