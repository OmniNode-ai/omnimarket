# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed host drift open item (OMN-17427)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._payload import decode_model, encode_model
from .enum_host_drift_verdict import EnumHostDriftVerdict


@dataclass(frozen=True, slots=True)
class ModelHostDriftOpenItem:
    key: str
    kind: str
    declared: str
    observed: str
    first_seen_at: datetime
    bound_seconds: int
    verdict: EnumHostDriftVerdict

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftOpenItem:
        return decode_model(cls, payload)
