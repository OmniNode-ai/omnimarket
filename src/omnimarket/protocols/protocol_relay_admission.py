# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Both writer boundaries inject the same core rule evaluator."""

from typing import Protocol

from omnimarket.models.model_relay_admission import (
    ModelRelayAdmissionResult,
    ModelRelayRequiresRule,
    ModelRelayRow,
)


class ProtocolRelayAdmission(Protocol):
    def evaluate(
        self, rule: ModelRelayRequiresRule, rows: tuple[ModelRelayRow, ...]
    ) -> ModelRelayAdmissionResult: ...
