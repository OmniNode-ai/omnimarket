# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the remote-lane guard limits."""

from pathlib import Path

from .handlers.handler_remote_lane_guard_limits import HandlerRemoteLaneGuardLimits


class NodeRemoteLaneGuardLimitsCompute(HandlerRemoteLaneGuardLimits):
    """Declarative entry point for the guard limit resolution."""

    contract_path = Path(__file__).with_name("contract.yaml")
