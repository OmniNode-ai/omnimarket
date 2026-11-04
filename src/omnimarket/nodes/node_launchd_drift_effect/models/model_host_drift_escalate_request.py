# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Open items and the clock for one escalation pass (OMN-20853)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._payload import decode_model, encode_model
from .model_host_drift_escalate_item import ModelHostDriftEscalateItem

# An open item escalates at four times its bound, and never later than six hours.
ESCALATION_BOUND_MULTIPLIER = 4
DEFAULT_ESCALATION_CAP_SECONDS = 21600


@dataclass(frozen=True, slots=True)
class ModelHostDriftEscalateRequest:
    items: tuple[ModelHostDriftEscalateItem, ...]
    now: datetime
    # The six-hour ceiling; a drill lowers it, the item bounds are never edited.
    cap_seconds: int = DEFAULT_ESCALATION_CAP_SECONDS

    def __post_init__(self) -> None:
        if self.cap_seconds <= 0:
            raise ValueError("cap_seconds must be positive")

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftEscalateRequest:
        return decode_model(cls, payload)
