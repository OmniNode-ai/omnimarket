# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lane guard admission."""

from pathlib import Path

from .handlers.handler_lane_guard_admission import HandlerLaneGuardAdmission


class NodeLaneGuardAdmissionCompute(HandlerLaneGuardAdmission):
    """Declarative entry point for lane guard admission operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
