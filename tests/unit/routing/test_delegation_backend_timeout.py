# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-backend timeout_ms resolution tests (OMN-13170).

Proves the routing authority resolves the per-backend HTTP timeout from the
bifrost delegation contract (no Python constant, no transport default) and that
the ms -> seconds conversion fails closed on a non-positive value, so a large
generation is no longer capped by the previously hardcoded 120s transport
default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.routing.delegation_backend_resolution import (
    resolve_delegation_backend,
    resolve_timeout_seconds,
)

_BIFROST_CONFIG_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)


def _backend(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "backend_id": "local-coder",
        "endpoint_url": "http://inference.example:8000/v1/chat/completions",
        "model_name": "Qwen3.6-35B-A3B",
        "tier": "local",
        "max_tokens": 65536,
        "timeout_ms": 60000,
        "capabilities": ["code_generation"],
    }
    base.update(overrides)
    return base


# --- Every committed backend declares timeout_ms --------------------------------


@pytest.mark.unit
def test_every_committed_backend_declares_timeout_ms() -> None:
    """Every backend in the committed contract carries a positive timeout_ms."""
    raw = yaml.safe_load(_BIFROST_CONFIG_PATH.read_text(encoding="utf-8"))
    for backend in raw["backends"]:
        config = ModelDelegationBackendConfig.model_validate(backend)
        assert config.timeout_ms >= 1, backend["backend_id"]


# --- resolve_delegation_backend carries timeout_ms / fails closed ---------------


@pytest.mark.unit
def test_resolve_backend_carries_timeout_ms() -> None:
    resolved = resolve_delegation_backend(
        "code_generation", backends=[_backend(timeout_ms=300000)]
    )
    assert resolved.timeout_ms == 300000


@pytest.mark.unit
def test_resolve_backend_fails_closed_when_timeout_ms_missing() -> None:
    backend = _backend()
    del backend["timeout_ms"]
    with pytest.raises(RuntimeError, match="timeout_ms"):
        resolve_delegation_backend("code_generation", backends=[backend])


@pytest.mark.unit
def test_resolve_backend_fails_closed_when_timeout_ms_non_positive() -> None:
    with pytest.raises(RuntimeError, match="timeout_ms"):
        resolve_delegation_backend("code_generation", backends=[_backend(timeout_ms=0)])


@pytest.mark.unit
def test_resolve_backend_fails_closed_when_timeout_ms_not_int() -> None:
    with pytest.raises(RuntimeError, match="timeout_ms"):
        resolve_delegation_backend(
            "code_generation", backends=[_backend(timeout_ms="60000")]
        )


# --- resolve_timeout_seconds: ms -> seconds, fail-closed on non-positive --------


@pytest.mark.unit
def test_resolve_timeout_seconds_divides_by_1000() -> None:
    assert resolve_timeout_seconds(backend_timeout_ms=300000) == 300.0


@pytest.mark.unit
def test_resolve_timeout_seconds_exceeds_old_hardcoded_cap() -> None:
    """A 300s contract timeout must resolve above the deleted 120s transport cap."""
    assert resolve_timeout_seconds(backend_timeout_ms=300000) > 120.0


@pytest.mark.unit
def test_resolve_timeout_seconds_rejects_non_positive() -> None:
    with pytest.raises(ValueError, match="backend_timeout_ms"):
        resolve_timeout_seconds(backend_timeout_ms=0)
