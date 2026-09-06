# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""Compatibility import for shared delegation routing config DTOs."""

from omnimarket.models.delegation.wire import (
    ModelDelegationConfig,
    parse_delegation_config_yaml,
)

__all__: list[str] = ["ModelDelegationConfig", "parse_delegation_config_yaml"]
