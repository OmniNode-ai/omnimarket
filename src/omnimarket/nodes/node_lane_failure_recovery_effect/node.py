# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entrypoint; the runtime owns bus dispatch."""

from .handlers.handler_lane_failure_recovery import HandlerLaneFailureRecovery


class NodeLaneFailureRecoveryEffect(HandlerLaneFailureRecovery):
    """Normalize receipts and contribute to the shared recovery plan."""
