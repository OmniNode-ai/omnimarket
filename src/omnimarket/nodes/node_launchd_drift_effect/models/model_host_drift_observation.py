# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed host drift observation (OMN-17427)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._payload import decode_model, encode_model
from .model_host_drift_item import ModelHostDriftItem


@dataclass(frozen=True, slots=True)
class ModelHostDriftObservation:
    host: str
    role: str
    observed_at: datetime
    items: tuple[ModelHostDriftItem, ...]

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftObservation:
        return decode_model(cls, payload)
