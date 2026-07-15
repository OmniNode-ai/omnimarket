# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Re-export shim — canonical definition moved to omnimarket.events.swarm_fanout (OMN-14586)."""

from omnimarket.events.swarm_fanout import (
    ModelSwarmFanoutResult as ModelSwarmFanoutResult,
)

__all__ = ["ModelSwarmFanoutResult"]
