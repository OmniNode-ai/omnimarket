# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point of the lane dispatch effect."""

from pathlib import Path

from .handlers.handler_lane_dispatch import HandlerLaneDispatch


class NodeLaneDispatchEffect(HandlerLaneDispatch):
    """Runtime entry point; topics and operation routing live in the contract."""

    contract_path = Path(__file__).with_name("contract.yaml")
