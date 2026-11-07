# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed host drift heartbeat event (OMN-17427)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._payload import decode_model, encode_model
from .model_host_drift_consumer_health import ModelHostDriftConsumerHealth
from .model_host_drift_open_item import ModelHostDriftOpenItem


@dataclass(frozen=True, slots=True)
class ModelHostDriftHeartbeatEvent:
    host: str
    role: str
    check_id: str
    checked_at: datetime
    interval_seconds: int
    items_total: int
    items_ok: int
    items_drift: int
    items_failed: int
    open: tuple[ModelHostDriftOpenItem, ...]
    previous_check_at: datetime | None
    gap_seconds: int | None
    manifest_schema: str = "host-drift-manifest.v1"
    generation: int | None = None
    consumer: ModelHostDriftConsumerHealth | None = None

    def to_payload(self) -> dict[str, object]:
        payload = encode_model(self)
        # Keep the slice-1 shape until every host can decode the extensions.
        for key in ("generation", "consumer"):
            if payload[key] is None:
                del payload[key]
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftHeartbeatEvent:
        return decode_model(cls, payload)
