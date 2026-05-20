# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verification bundle produced by the verifier subagent.

Captures the full evidence set from one worker+verifier round trip.

Related:
    - OMN-11220: Verification-First Parallel Worker Dispatch Skill
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelAuthoritativeCheck(BaseModel):
    """A single authoritative surface queried by the verifier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: str = Field(
        ...,
        description="Authoritative surface queried (e.g. github_pr, ci_checks, linear).",
    )
    query: str = Field(
        ..., description="Query or command executed against the surface."
    )
    result: str = Field(..., description="Raw result from the surface.")
    passed: bool = Field(
        ..., description="True when the surface confirmed the worker's claim."
    )


class ModelDetectedMismatch(BaseModel):
    """A mismatch detected between worker claim and authoritative surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: str = Field(
        ..., description="Authoritative surface where mismatch was found."
    )
    worker_claim: str = Field(..., description="What the worker claimed.")
    actual_state: str = Field(
        ..., description="What the authoritative surface reported."
    )
    severity: Literal["critical", "major", "minor"] = Field(
        ..., description="Mismatch severity."
    )


class ModelVerificationBundle(BaseModel):
    """Full evidence bundle from one worker+verifier round trip.

    Produced by the verifier subagent after independently querying all
    authoritative surfaces declared in the worker's self-report.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    worker_run_id: str = Field(
        ..., description="Correlation ID of the worker run being verified."
    )
    verifier_run_id: str = Field(
        ..., description="Correlation ID of this verifier run."
    )
    claim: str = Field(..., description="The worker's top-level claim being verified.")
    authoritative_checks: list[ModelAuthoritativeCheck] = Field(
        default_factory=list,
        description="All authoritative surface checks executed by the verifier.",
    )
    detected_mismatches: list[ModelDetectedMismatch] = Field(
        default_factory=list,
        description="All mismatches detected between worker claims and authoritative surfaces.",
    )
    decision: Literal["accept", "reject"] = Field(
        ...,
        description="Verifier decision: accept when all checks pass, reject otherwise.",
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="Paths or URLs to supporting evidence artifacts.",
    )
    timestamp_utc: datetime = Field(
        ..., description="UTC timestamp when this bundle was produced."
    )
    correlation_id: str = Field(
        ...,
        description="Shared correlation ID linking worker run, verifier run, and dispatch attempt.",
    )


__all__: list[str] = [
    "ModelAuthoritativeCheck",
    "ModelDetectedMismatch",
    "ModelVerificationBundle",
]
