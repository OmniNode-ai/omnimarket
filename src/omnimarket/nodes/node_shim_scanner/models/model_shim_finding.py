# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Finding model for a single @shim annotation discovered by node_shim_scanner."""

from __future__ import annotations

import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = ["EnumShimStatus", "ModelShimFinding"]


class EnumShimStatus(StrEnum):
    EXPIRED = "EXPIRED"
    EXPIRING = "EXPIRING"
    ACTIVE = "ACTIVE"


class ModelShimFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str
    line_number: int
    function_name: str
    ticket_id: str
    expires_on: datetime.date
    reason: str
    replacement: str
    status: EnumShimStatus
    days_until_expiry: int
