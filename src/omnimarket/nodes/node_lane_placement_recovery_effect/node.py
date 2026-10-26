# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thin lane placement recovery node; receipt and lane queue behavior lives in its handler."""

from __future__ import annotations

from pathlib import Path

from .handlers.handler_lane_engine_failover import (
    engine_admission,
    engine_readings,
    host_admission,
    initialize_receipt,
    parse_route,
)
from .handlers.handler_lane_placement_recovery import HandlerLanePlacementRecovery


class NodeLanePlacementRecoveryEffect(HandlerLanePlacementRecovery):
    """Local entry point for the operations declared in contract.yaml."""

    contract_path = Path(__file__).with_name("contract.yaml")

    engine_readings = staticmethod(engine_readings)
    engine_admission = staticmethod(engine_admission)
    host_admission = staticmethod(host_admission)
    initialize_receipt = staticmethod(initialize_receipt)
    parse_route = staticmethod(parse_route)
    tick_request = HandlerLanePlacementRecovery.codex_tick_facts
    placement_request = HandlerLanePlacementRecovery.codex_placement_facts
