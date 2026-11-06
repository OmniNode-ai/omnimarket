# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative node for the census/alarm alias parity check."""

from __future__ import annotations

from pathlib import Path

from .handler import HandlerLaneCensusAliasParity
from .models import ModelLaneCensusAliasParityRequest, ModelLaneCensusAliasParityResult

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeLaneCensusAliasParityCompute:
    contract_path = CONTRACT

    def handle(
        self, request: ModelLaneCensusAliasParityRequest
    ) -> ModelLaneCensusAliasParityResult:
        return HandlerLaneCensusAliasParity().handle(request)
