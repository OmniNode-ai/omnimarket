# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR state observation as the orchestrator reads it: the fields it uses, nothing it refuses (OMN-26721)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneWorkObservation(BaseModel):
    """Accepts the watcher's observation of either schema version; strict enums and tuples of
    the emitting model are not required of a JSON wire message."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    repo: str = Field(..., min_length=1)
    pr_number: int = Field(..., gt=0)
    head_sha: str = Field(..., min_length=7)
    ci_verdict: str = Field(..., min_length=1)
    ci_read_at: str
    observed_at: str
    digest: str = ""


__all__: list[str] = ["ModelLaneWorkObservation"]
