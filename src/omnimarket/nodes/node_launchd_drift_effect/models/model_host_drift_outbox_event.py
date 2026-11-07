# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed host drift outbox event (OMN-17427)."""

from __future__ import annotations

from dataclasses import dataclass

from ._payload import decode_model, encode_model


@dataclass(frozen=True, slots=True)
class ModelHostDriftOutboxEvent:
    topic: str
    key: str
    payload: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftOutboxEvent:
        return decode_model(cls, payload)
