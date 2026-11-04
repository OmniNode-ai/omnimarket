# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The exception queue's ledger detector request and readback."""

from __future__ import annotations

from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict


class ModelAndonLedgerHoldsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state_dir: Path
    ledger_path: Path
    now: AwareDatetime


class ModelAndonLedgerHoldsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_ids: tuple[str, ...] = ()
    reports: tuple[str, ...] = ()
