# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One commitment, folded from its ledger rows."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.commitment.enum_commitment_status import (
    EnumCommitmentStatus,
)


class ModelCommitment(BaseModel):
    """The ask verbatim, who owes it, the check that proves it on the running system, and the newest reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commitment_id: str
    ask: str
    owner: str
    check: str
    ticket: str | None = None
    # References that must be done before the commitment can start (a ticket, a pull request or a reading).
    requires: tuple[str, ...] = ()
    status: EnumCommitmentStatus
    opened_at: datetime
    last_checked_at: datetime | None = None
    last_exit: int | None = None
    regressions: int = 0
