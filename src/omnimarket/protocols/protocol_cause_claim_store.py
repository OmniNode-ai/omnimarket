# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Part A claim-store crossing: insert the key atomically and return its holder."""

from typing import Protocol, runtime_checkable

from omnimarket.models.standing_rules.model_rule_cause_idle import (
    ModelCauseReservation,
    ModelCauseReservationResult,
)


@runtime_checkable
class ProtocolCauseClaimStore(Protocol):
    def reserve(self, claim: ModelCauseReservation) -> ModelCauseReservationResult:
        """Unique-key insert, returning its insertion status and holder."""
        ...
