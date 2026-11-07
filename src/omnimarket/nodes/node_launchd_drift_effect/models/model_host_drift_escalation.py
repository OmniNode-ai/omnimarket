# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed verdict of a host drift item open past its escalation age (OMN-20853)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._payload import decode_model, encode_model
from .enum_host_drift_verdict import EnumHostDriftVerdict


@dataclass(frozen=True, slots=True)
class ModelHostDriftEscalation:
    # Deterministic: sha256 of host, key and first_seen_at, first 16 hex characters. One episode,
    # one id, so a consumer raises one alarm however many runs repeat the verdict.
    episode_id: str
    host: str
    key: str
    kind: str
    verdict: EnumHostDriftVerdict
    observed: str
    first_seen_at: datetime
    # first_seen_at plus escalate_after_seconds: a function of the episode, not of the run.
    escalated_at: datetime
    escalate_after_seconds: int
    age_seconds: int
    apply_class: str
    # The exact operator command for an item no fix node repairs; empty when the fix node owns it.
    command: str
    fix_attempts: int
    fix_outcome: str
    fix_detail: str

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftEscalation:
        return decode_model(cls, payload)
