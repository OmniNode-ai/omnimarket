# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read boundary of the unjudged-row reading (OMN-23152)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_unjudged_reading import (
    ModelUnjudgedSourceCount,
)


class ProtocolUnjudgedRowReader(Protocol):
    """Counts the projected rows with no ledger_seq first projected at or after ``since``."""

    def unjudged_by_source(
        self, *, ledger_id: str, since: datetime
    ) -> list[ModelUnjudgedSourceCount]: ...


__all__: list[str] = ["ProtocolUnjudgedRowReader"]
