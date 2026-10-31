# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Composition adapter: the timeouts a node declares in its packaged ``contract.yaml``.

A node lists its durations under a top-level ``timeouts`` mapping. A missing or
malformed declaration refuses at composition; the package supplies no fallback value.
"""

from __future__ import annotations

from functools import cache

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.models.model_node_timeouts import ModelNodeTimeouts

TIMEOUTS_SECTION = "timeouts"


@cache
def load_node_timeouts(node_name: str) -> ModelNodeTimeouts:
    """The durations ``node_name`` declares; raises when the contract declares none."""
    return ModelNodeTimeouts.model_validate(
        {"seconds": load_packaged_contract_section(node_name, TIMEOUTS_SECTION)}
    )


__all__ = ["TIMEOUTS_SECTION", "load_node_timeouts"]
