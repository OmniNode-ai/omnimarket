# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The triage fence derivation as a compute node (OMN-24248, moved from OMN-20682).

``HandlerTriageFencesDerive.handle(ModelTriageFencesDeriveRequest) -> ModelTriageFencesVerdict``.
"""

from omnimarket.nodes.node_triage_fences_compute.handlers.handler_triage_fences_derive import (
    HandlerTriageFencesDerive,
)


class NodeTriageFencesCompute(HandlerTriageFencesDerive):
    """ONEX entry-point wrapper for HandlerTriageFencesDerive."""


__all__ = ["HandlerTriageFencesDerive", "NodeTriageFencesCompute"]
