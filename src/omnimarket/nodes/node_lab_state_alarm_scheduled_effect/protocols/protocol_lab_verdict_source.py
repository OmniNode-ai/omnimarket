# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam the lab state alarm scheduled effect reads its verdicts from."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from omnimarket.models.floor_17a.model_floor_17a import ModelLabSourceVerdict


@runtime_checkable
class ProtocolLabVerdictSource(Protocol):
    """Reads the lab source verdicts at an instant; ``None`` means the lab sources could not be read."""

    def read(self, now: datetime) -> tuple[ModelLabSourceVerdict, ...] | None: ...


__all__: list[str] = [
    "ProtocolLabVerdictSource",
]
