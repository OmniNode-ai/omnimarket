# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane guard unit effect: start, list and stop a lane engine inside onexlane-<lane>-<id>."""

from pathlib import Path

from .handlers.handler_lane_guard_unit import HandlerLaneGuardUnit


class NodeLaneGuardUnitEffect(HandlerLaneGuardUnit):
    """Declarative entry point for lane guard unit operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
