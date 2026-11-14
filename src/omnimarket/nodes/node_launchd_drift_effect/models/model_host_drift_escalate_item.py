# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One open host drift item as the escalation rule reads it (OMN-20853)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._payload import decode_model, encode_model
from .enum_host_drift_verdict import EnumHostDriftVerdict


@dataclass(frozen=True, slots=True)
class ModelHostDriftEscalateItem:
    host: str
    key: str
    kind: str
    verdict: EnumHostDriftVerdict
    observed: str
    first_seen_at: datetime
    bound_seconds: int
    # The sync class the policy gave the item and the exact operator command for it.
    apply_class: str
    command: str = ""
    fix_attempts: int = 0
    fix_outcome: str = ""
    fix_detail: str = ""

    def to_payload(self) -> dict[str, object]:
        return encode_model(self)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ModelHostDriftEscalateItem:
        return decode_model(cls, payload)
