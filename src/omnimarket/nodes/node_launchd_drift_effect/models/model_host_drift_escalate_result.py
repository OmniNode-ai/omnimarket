# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Items past their escalation age (OMN-20853)."""

from __future__ import annotations

from dataclasses import dataclass

from ._payload import decode_model, encode_model
from .model_host_drift_escalation import ModelHostDriftEscalation


@dataclass(frozen=True, slots=True)
class ModelHostDriftEscalateResult:
    escalations: tuple[ModelHostDriftEscalation, ...]

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftEscalateResult:
        return decode_model(cls, payload)
