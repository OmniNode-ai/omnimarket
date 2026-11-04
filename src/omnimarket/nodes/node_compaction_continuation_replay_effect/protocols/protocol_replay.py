# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fixed answerer can propose calls but never execute them."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.compaction_measurement.action import (
    ModelActionScoreRequest,
    ModelActionScoreResult,
)

from ..models.model_experiment import ModelAssistantTurn, ModelContinuationInput


class ProtocolContinuationModel(Protocol):
    def continue_turn(self, request: ModelContinuationInput) -> ModelAssistantTurn: ...


class ProtocolActionScorer(Protocol):
    def handle(self, request: ModelActionScoreRequest) -> ModelActionScoreResult: ...
