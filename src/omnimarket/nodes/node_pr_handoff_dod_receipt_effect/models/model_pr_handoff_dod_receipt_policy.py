# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrHandoffDodReceiptPolicy for the handoff receipt check."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelPrHandoffDodReceiptPolicy(BaseModel):
    """All storage conventions and gate values are supplied by the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_pattern: str
    head_pattern: str
    receipt_pattern: str
    receipt_root_env: str
    evidence_root_env: str
    state_home_key: str
    summary_name: str
    passing_status: str
    admitted_code: int
    refused_code: int
    unreadable_code: int
