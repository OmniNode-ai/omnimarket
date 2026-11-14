# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What reading a ticket body against the standard form found."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelTicketBodyReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_present: bool
    found_fingerprint: str = ""
    differences: tuple[str, ...] = ()
