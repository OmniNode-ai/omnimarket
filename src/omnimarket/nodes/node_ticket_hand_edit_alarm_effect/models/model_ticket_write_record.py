# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The newest written event the node keeps for one ticket."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelTicketWriteRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    contract_fingerprint: str
    content_sha256: str
    linear_updated_at: datetime
