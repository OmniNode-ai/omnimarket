# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reader's planned lanes and their prepared, serialized dispatch records.

The caller supplies fresh ticket and claim facts, generated constraints, a pinned
revision and resolved placement in each record. The dispatcher never invents
those facts or a repository for a fleet-wide diagnosis. A plan may omit its host
pin; its record still needs the host chosen by placement.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lane_dispatch.model_lane_dispatch_effect import (
    ModelLaneDispatchResult,
)


class ModelMergeSweepDispatchLane(BaseModel):
    """One reader-planned lane, with the exact record prepared for that lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    repo: str | None = None
    ticket: str = Field(min_length=1)
    prs: tuple[str, ...] = ()
    host: str | None = None
    host_why: str = ""
    record_json: str = Field(min_length=1)


class ModelMergeSweepDispatchRequest(BaseModel):
    """Dispatch only a bounded prefix, keeping the plan's lane identities intact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parent: str = Field(min_length=1)
    lanes: tuple[ModelMergeSweepDispatchLane, ...]
    output_dir: str = Field(min_length=1)
    max_lanes: int = Field(default=8, ge=0, le=12)
    execute: bool = False


class ModelMergeSweepDispatchOutcome(ModelLaneDispatchResult):
    """The dispatch receipt, not a claim that the lane finished or its PR landed."""

    lane: str
    kind: str
    repo: str | None
    ticket: str
    host: str | None


class ModelMergeSweepDispatchResult(BaseModel):
    """Each attempted lane's admission or submission, with unattempted tail count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    outcomes: tuple[ModelMergeSweepDispatchOutcome, ...]
    deferred: int
