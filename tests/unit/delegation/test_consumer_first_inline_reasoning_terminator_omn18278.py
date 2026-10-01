# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18278: the backend config now declares ``inline_reasoning_terminator``.

The consumer-first release accepted the key and dropped it. This change declares
it, so a decoded backend keeps the terminator instead of discarding it.
"""

from __future__ import annotations

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)


def test_backend_config_keeps_inline_reasoning_terminator() -> None:
    cfg = ModelDelegationBackendConfig.model_validate(
        {
            "backend_id": "local-reasoner",
            "tier": "local",
            "inline_reasoning_terminator": "</think>",
        }
    )
    assert cfg.model_dump()["inline_reasoning_terminator"] == "</think>"
