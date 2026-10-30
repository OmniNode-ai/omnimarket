# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for plans-today."""

from pathlib import Path

from .handlers.handler_plans_today import HandlerPlansToday


class NodePlansTodayEffect(HandlerPlansToday):
    """Declarative entry point for the plans of the day."""

    contract_path = Path(__file__).with_name("contract.yaml")
