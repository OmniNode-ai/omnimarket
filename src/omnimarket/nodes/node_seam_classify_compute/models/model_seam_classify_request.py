# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of node_seam_classify_compute (OMN-21459)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.seam_reading import ModelSeamReading


class ModelSeamClassifyRequest(BaseModel):
    """One seam operation, the facts it classifies and ``now``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str
    facts: Mapping[str, object]
    now: datetime


class ModelSeamClassifyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    readings: tuple[ModelSeamReading, ...]
