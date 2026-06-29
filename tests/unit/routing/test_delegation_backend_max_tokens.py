# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-backend max_tokens resolution tests (OMN-13161).

Proves the routing authority resolves the per-backend output-token budget from the
bifrost delegation contract (no Python constant, no env var) and that the effective
value follows the unset->backend / explicit->capped rule.
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
    resolve_effective_max_tokens,
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


# --- ModelDelegationBackendConfig parses max_tokens from YAML --------------------


@pytest.mark.unit
def test_backend_config_parses_max_tokens_field() -> None:
    config = ModelDelegationBackendConfig.model_validate(
        {
            "backend_id": "local-coder",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 65536,
        }
    )
    assert config.max_tokens == 65536


@pytest.mark.unit
def test_local_coder_resolves_to_65536_from_committed_config() -> None:
    """The committed bifrost contract declares local-coder max_tokens=65536."""
    raw = yaml.safe_load(_BIFROST_CONFIG_PATH.read_text(encoding="utf-8"))
    backends_by_id = {b["backend_id"]: b for b in raw["backends"]}

    local_coder = ModelDelegationBackendConfig.model_validate(
        backends_by_id["local-coder"]
    )
    assert local_coder.max_tokens == 65536


@pytest.mark.unit
def test_every_committed_backend_declares_max_tokens() -> None:
    """Every backend in the committed contract carries an explicit max_tokens."""
    raw = yaml.safe_load(_BIFROST_CONFIG_PATH.read_text(encoding="utf-8"))
    for backend in raw["backends"]:
        config = ModelDelegationBackendConfig.model_validate(backend)
        assert config.max_tokens >= 1, backend["backend_id"]


# --- resolve_delegation_backend carries max_tokens / fails closed ---------------


@pytest.mark.unit
def test_resolve_backend_carries_max_tokens() -> None:
    resolved = resolve_delegation_backend(
        "code_generation", backends=[_backend(max_tokens=65536)]
    )
    assert resolved.max_tokens == 65536


@pytest.mark.unit
def test_resolve_backend_fails_closed_when_max_tokens_missing() -> None:
    backend = _backend()
    del backend["max_tokens"]
    with pytest.raises(RuntimeError, match="max_tokens"):
        resolve_delegation_backend("code_generation", backends=[backend])


@pytest.mark.unit
def test_resolve_backend_fails_closed_when_max_tokens_non_positive() -> None:
    with pytest.raises(RuntimeError, match="max_tokens"):
        resolve_delegation_backend("code_generation", backends=[_backend(max_tokens=0)])


# --- resolve_effective_max_tokens: unset -> backend, explicit -> capped ---------


@pytest.mark.unit
def test_effective_max_tokens_unset_uses_backend_ceiling() -> None:
    assert (
        resolve_effective_max_tokens(requested=None, backend_max_tokens=65536) == 65536
    )


@pytest.mark.unit
def test_effective_max_tokens_explicit_below_ceiling_passes_through() -> None:
    assert (
        resolve_effective_max_tokens(requested=4096, backend_max_tokens=65536) == 4096
    )


@pytest.mark.unit
def test_effective_max_tokens_explicit_above_ceiling_is_capped() -> None:
    assert (
        resolve_effective_max_tokens(requested=200000, backend_max_tokens=8192) == 8192
    )


@pytest.mark.unit
def test_effective_max_tokens_rejects_non_positive_backend_ceiling() -> None:
    with pytest.raises(ValueError, match="backend_max_tokens"):
        resolve_effective_max_tokens(requested=None, backend_max_tokens=0)


@pytest.mark.unit
def test_effective_max_tokens_rejects_non_positive_request() -> None:
    with pytest.raises(ValueError, match="requested"):
        resolve_effective_max_tokens(requested=0, backend_max_tokens=65536)
