# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed host drift resolved event (OMN-17427)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._payload import decode_model, encode_model
from .enum_host_drift_resolution import EnumHostDriftResolution


@dataclass(frozen=True, slots=True)
class ModelHostDriftResolvedEvent:
    host: str
    role: str
    check_id: str
    key: str
    kind: str
    declared: str
    last_observed: str
    first_seen_at: datetime
    resolved_at: datetime
    duration_seconds: int
    resolution: EnumHostDriftResolution

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftResolvedEvent:
        return decode_model(cls, payload)
