# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18278, consumer first: a released consumer must decode the next shape.

The change after this consumer's release declares ``inline_reasoning_terminator``
on a backend in the bifrost delegation config. A consumer that forbids extra
keys would dead-letter that payload, so this release accepts the key.
"""

from __future__ import annotations

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)


def test_backend_config_accepts_inline_reasoning_terminator() -> None:
    cfg = ModelDelegationBackendConfig.model_validate(
        {
            "backend_id": "local-reasoner",
            "tier": "local",
            "inline_reasoning_terminator": "</think>",
        }
    )
    assert cfg.inline_reasoning_terminator == "</think>"


def test_backend_config_terminator_defaults_to_none() -> None:
    cfg = ModelDelegationBackendConfig.model_validate(
        {"backend_id": "local-reasoner", "tier": "local"}
    )
    assert cfg.inline_reasoning_terminator is None
