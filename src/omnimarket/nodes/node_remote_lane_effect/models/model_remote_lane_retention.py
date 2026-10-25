# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host closeout archival and retention requests."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelRemoteLaneRetentionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    root: str
    receipt: dict[str, Any]
    ledger: str = Field(min_length=1)
    keep_days: int = Field(default=7, ge=1, strict=True)
    delete: bool = False


class ModelRemoteLaneRetentionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    archive: str
    removed: list[str]
    pruned: list[str]
