# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18278, consumer first: a released consumer must decode the next shape.

The change after this consumer's release declares ``inline_reasoning_terminator``
on a backend in the bifrost delegation config. A consumer that forbids extra
keys would dead-letter that payload, so this release accepts the key and drops it.
"""

from __future__ import annotations

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)


def test_backend_config_accepts_and_drops_inline_reasoning_terminator() -> None:
    cfg = ModelDelegationBackendConfig.model_validate(
        {
            "backend_id": "local-reasoner",
            "tier": "local",
            "inline_reasoning_terminator": "</think>",
        }
    )
    assert "inline_reasoning_terminator" not in cfg.model_dump()
