# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect node of the failure alarm router (OMN-26483)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ModelFailureAlarmRequest, ModelFailureAlarmResult

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeFailureAlarmRouterEffect:
    contract_path = CONTRACT

    def handle(self, request: ModelFailureAlarmRequest) -> ModelFailureAlarmResult:
        from .handler import HandlerFailureAlarmRouter

        return HandlerFailureAlarmRouter().handle(request)
