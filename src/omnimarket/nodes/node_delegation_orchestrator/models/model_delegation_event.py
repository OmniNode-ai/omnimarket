# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Compatibility import for the canonical delegation event envelope."""

from __future__ import annotations

from omnimarket.models.delegation.wire import (
    ModelDelegationEventEnvelope as ModelDelegationEvent,
)

__all__: list[str] = ["ModelDelegationEvent"]
