# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the review key credit probe (OMN-24658, OMN-24727)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelReviewKeyProbeConfig(BaseModel):
    """The probe's one request, read from the node contract's ``config.probe`` (OMN-24727).

    Never defaulted: a contract that omits a field refuses to load.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tokens: Annotated[int, Field(ge=1, strict=True)]
    prompt: Annotated[str, Field(min_length=1)]


class ModelReviewKeyCreditProbeRequest(BaseModel):
    """One probe run: which tenant's keys on which host, and where the state is kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    model: str
    host: str
    thinking_mode: Literal["disabled", "adaptive"] = "disabled"
    state_dir: Path


class ModelReviewKeyCreditProbeResult(BaseModel):
    """Each key's display name with its credit state, and the flips written to the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    states: tuple[tuple[str, str], ...]
    flips_written: int
