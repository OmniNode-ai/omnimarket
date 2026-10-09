# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DoD closeout sweep decision compute node (OMN-20675)."""

from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)


class NodeDodCloseoutSweepCompute(HandlerDodCloseoutSweep):
    """ONEX entrypoint for the DoD closeout sweep's deterministic decisions."""


__all__ = ["HandlerDodCloseoutSweep", "NodeDodCloseoutSweepCompute"]
