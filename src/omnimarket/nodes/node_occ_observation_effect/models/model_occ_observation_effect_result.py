# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccObservationEffectResult — the OCC observation append-write outcome (OMN-14888)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelOccObservationEffectResult(BaseModel):
    """The outcome of an OCC observation append write-EFFECT run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["dry_run", "mutate"] = Field(..., description="Run mode.")
    action: str = Field(..., description="Human-readable summary of what happened.")
    relpath: str = Field(
        ..., description="The deterministic repo-relative path this record maps to."
    )
    already_present: bool = Field(
        default=False,
        description="True when the exact append-only path already existed on the "
        "OCC branch (idempotent no-op re-ingestion of the same attempt).",
    )
    superseded_by_open_pr: bool = Field(
        default=False,
        description="True when an OPEN observation PR for the same content "
        "identity (same product repo/PR/head_sha/policy_version, differing only "
        "by workflow run) already existed, so this run opened no second PR "
        "(OMN-15300 duplicate-emission guard).",
    )
    occ_branch: str = Field(default="", description="Deterministic OCC append branch.")
    occ_pr_number: int | None = Field(
        default=None, description="OCC observation PR number (opened or reused)."
    )
    occ_pr_url: str = Field(default="", description="OCC observation PR HTML URL.")


__all__ = ["ModelOccObservationEffectResult"]
