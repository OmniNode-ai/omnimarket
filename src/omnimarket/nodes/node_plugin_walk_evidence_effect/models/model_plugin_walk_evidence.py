# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed plugin-walk evidence request and result."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class ModelPluginWalkEvidenceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    positive: Path
    negative: Path
    database: Path


class ModelPluginWalkEvidenceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    positive_correlation_ids: tuple[str, ...]
    negative_correlation_ids: tuple[str, ...] = ()
    readback: dict[str, Any]
