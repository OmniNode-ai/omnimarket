# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed host drift item (OMN-17427)."""

from __future__ import annotations

from dataclasses import dataclass

from ._payload import decode_model, encode_model
from .enum_host_drift_verdict import EnumHostDriftVerdict


@dataclass(frozen=True, slots=True)
class ModelHostDriftItem:
    key: str
    kind: str
    verdict: EnumHostDriftVerdict
    declared: str
    observed: str
    bound_seconds: int

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftItem:
        return decode_model(cls, payload)
