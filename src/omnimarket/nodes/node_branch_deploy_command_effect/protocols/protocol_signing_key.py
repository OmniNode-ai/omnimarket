# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The signing key protocol of node_branch_deploy_command_effect."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_branch_deploy_command_effect.models.model_signing_key_reading import (
    ModelSigningKeyReading,
)


class ProtocolSigningKey(Protocol):
    """The signing key port the handler takes as a constructor argument; with none, the variable named by the
    overlay is the only source and behaves as before (OMN-24611)."""

    async def read(self) -> ModelSigningKeyReading: ...
