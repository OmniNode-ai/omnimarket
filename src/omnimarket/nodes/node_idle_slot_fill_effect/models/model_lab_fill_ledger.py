# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed ledger acquisition of idle-slot fill."""

from __future__ import annotations

from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict


class ModelLabFillLedgerRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    ledger: Path
    drain_map: Path
    registry_root: Path


class ModelLabFillLedgerResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claimed: frozenset[str]
    readbacks: dict[str, str]
