# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read whether a host unit is loaded and when it last ran."""

from pathlib import Path

from .handlers.handler_unit_probe import HandlerUnitProbe
from .models.model_unit_probe import ModelUnitProbeRequest, ModelUnitProbeResult


class NodeUnitProbeEffect:
    """ONEX entry point for the host-unit probe; process and ssh behavior lives in its handler."""

    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerUnitProbe()

    def handle(self, request: ModelUnitProbeRequest) -> ModelUnitProbeResult:
        return self.handler.handle(request)


__all__ = [
    "HandlerUnitProbe",
    "ModelUnitProbeRequest",
    "ModelUnitProbeResult",
    "NodeUnitProbeEffect",
]
