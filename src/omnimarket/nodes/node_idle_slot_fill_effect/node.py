# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect node of the idle-slot fill and the idle-turn guard (OMN-17427)."""

from __future__ import annotations

from pathlib import Path

from .handlers.handler_idle_slot_fill import HandlerIdleSlotFill


class NodeIdleSlotFillEffect(HandlerIdleSlotFill):
    """Local entry point for the operations declared in contract.yaml."""

    contract_path = Path(__file__).with_name("contract.yaml")
