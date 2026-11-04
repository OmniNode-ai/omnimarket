# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed host drift fold result (OMN-17427)."""

from __future__ import annotations

from dataclasses import dataclass

from ._payload import decode_model, encode_model
from .model_host_drift_detected_event import ModelHostDriftDetectedEvent
from .model_host_drift_heartbeat_event import ModelHostDriftHeartbeatEvent
from .model_host_drift_resolved_event import ModelHostDriftResolvedEvent
from .model_host_drift_state import ModelHostDriftState


@dataclass(frozen=True, slots=True)
class ModelHostDriftFoldResult:
    state: ModelHostDriftState
    detected: tuple[ModelHostDriftDetectedEvent, ...]
    resolved: tuple[ModelHostDriftResolvedEvent, ...]
    heartbeat: ModelHostDriftHeartbeatEvent

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftFoldResult:
        return decode_model(cls, payload)
