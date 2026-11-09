# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative node for the start-path census and refusal drill."""

from __future__ import annotations

from pathlib import Path

from .handler import HandlerStartPathCensus
from .models import ModelStartPathCensusRequest, ModelStartPathCensusResult

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeStartPathCensusCompute:
    contract_path = CONTRACT

    def handle(
        self, request: ModelStartPathCensusRequest
    ) -> ModelStartPathCensusResult:
        return HandlerStartPathCensus().handle(request)
