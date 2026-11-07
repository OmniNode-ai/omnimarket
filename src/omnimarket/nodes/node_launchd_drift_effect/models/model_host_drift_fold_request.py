# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed host drift fold request (OMN-17427)."""

from __future__ import annotations

from dataclasses import dataclass

from ._payload import decode_model, encode_model
from .model_host_drift_observation import ModelHostDriftObservation
from .model_host_drift_state import ModelHostDriftState


@dataclass(frozen=True, slots=True)
class ModelHostDriftFoldRequest:
    previous: ModelHostDriftState
    observation: ModelHostDriftObservation
    check_id: str
    interval_seconds: int
    generation: int = 0
    heartbeat_extensions: bool = False
    checked_keys: tuple[str, ...] | None = None

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftFoldRequest:
        return decode_model(cls, payload)
