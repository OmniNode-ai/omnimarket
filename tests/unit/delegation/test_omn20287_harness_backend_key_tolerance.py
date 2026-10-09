# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-first tolerance of the harness backend keys (OMN-20287).

A later release declares ``kind``, ``harness``, ``surface`` and
``tenant_scope`` on ``ModelDelegationBackendConfig``. This release must decode
a backend carrying them instead of refusing it, drop exactly those keys, and
still refuse every other unknown key.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)

_BASE: dict[str, Any] = {
    "backend_id": "harness-x",
    "provider": "anthropic",
    "model_name": "claude-sonnet-5-5",
    "tier": "harness",
}
_FORTHCOMING: dict[str, Any] = {
    "kind": "harness",
    "harness": "claude",
    "surface": "internal",
    "tenant_scope": "house",
}


@pytest.mark.unit
def test_backend_with_forthcoming_harness_keys_decodes() -> None:
    backend = ModelDelegationBackendConfig.model_validate({**_BASE, **_FORTHCOMING})
    assert backend.backend_id == "harness-x"
    assert backend == ModelDelegationBackendConfig.model_validate(_BASE)


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(_FORTHCOMING))
def test_each_forthcoming_key_is_dropped_alone(key: str) -> None:
    backend = ModelDelegationBackendConfig.model_validate(
        {**_BASE, key: _FORTHCOMING[key]}
    )
    assert key not in backend.model_dump()


@pytest.mark.unit
def test_other_unknown_keys_are_still_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelDelegationBackendConfig.model_validate(
            {**_BASE, **_FORTHCOMING, "executor": "node_coding_agent_invoke_effect"}
        )
