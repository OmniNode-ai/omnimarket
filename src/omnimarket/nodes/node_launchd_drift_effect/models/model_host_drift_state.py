# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed host drift state (OMN-17427)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._payload import decode_model, encode_model
from .model_host_drift_open_item import ModelHostDriftOpenItem
from .model_host_drift_outbox_event import ModelHostDriftOutboxEvent


@dataclass(frozen=True, slots=True)
class ModelHostDriftState:
    host: str
    last_check_at: datetime | None
    open: tuple[ModelHostDriftOpenItem, ...]
    generation: int = 0
    outbox: tuple[ModelHostDriftOutboxEvent, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftState:
        return decode_model(cls, payload)
