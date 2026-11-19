# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect-node bypass ratchet compute (OMN-24432): one repository's bypass count against its shrink-only baseline.

``HandlerBypassRatchet.handle(ModelBypassRatchetRequest) -> ModelBypassRatchetVerdict`` (definition-B). Pure; the scan is
node_effect_bypass_scan_effect.
"""

from omnimarket.nodes.node_effect_bypass_ratchet_compute.handlers.handler_bypass_ratchet import (
    HandlerBypassRatchet,
)


class NodeEffectBypassRatchetCompute(HandlerBypassRatchet):
    """ONEX entry-point wrapper for HandlerBypassRatchet."""


__all__ = ["HandlerBypassRatchet", "NodeEffectBypassRatchetCompute"]
