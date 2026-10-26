# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_context_adr_select_compute: the ADR tier of the context service (OMN-21126).

Pure COMPUTE node: admits accepted, unsuperseded ADRs tagged with a profile's
topics, verbatim from their bodies at a pinned commit, and lists exclusions.
No token budget and no pack hash.
"""

from omnimarket.nodes.node_context_adr_select_compute.handlers import (
    HandlerContextAdrSelect,
)

__all__ = ["HandlerContextAdrSelect"]
