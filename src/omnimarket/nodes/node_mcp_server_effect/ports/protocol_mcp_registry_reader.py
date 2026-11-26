# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registry reader the tools/list handler depends on."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnimarket.nodes.node_mcp_server_effect.models.model_mcp_tools_list import (
    ModelMcpRegistryRow,
)


class ProtocolMcpRegistryReader(Protocol):
    """Reads every registration row in full, never a capped page."""

    def read_registrations(self) -> Sequence[ModelMcpRegistryRow]: ...
