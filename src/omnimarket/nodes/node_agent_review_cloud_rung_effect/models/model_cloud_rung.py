# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the cloud rung: the vote request, the overlay, the cloud lane run it reads,
the delegation event it emits and the unsigned vote it returns.

The vote has the shape of the dispatch orchestrator's voter review (identity, endpoint,
model, delegation event id, findings); this package keeps its own copy because a node
never imports another node's models. It has no signature field and forbids extra keys:
the rung never signs and never passes on a signature a session wrote.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.agent_review_cloud import ModelCloudReview

_REPO = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_SHA = r"^[0-9a-f]{40}$"


class EnumCloudRungTransport(StrEnum):
    """The bus the delegation event travels on."""

    IN_MEMORY = "in_memory"
    REDPANDA = "redpanda"


class ModelCloudRungConfig(BaseModel):
    """``<root>/node_agent_review_cloud_rung_effect/overlay.yaml``: every deployment fact of the rung."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: EnumCloudRungTransport
    ticket: str = Field(pattern=r"^OMN-\d+$")
    parent: str = Field(min_length=1)
    lane_prefix: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
    runner_model: str = Field(min_length=1)
    timeout_min: int = Field(ge=10, le=170)
    harness: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    bus_lane: str = Field(min_length=1)


class ModelCloudVoteRequest(BaseModel):
    """One voter's review of one pull request head; the session checks the head out itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str = Field(min_length=1)
    model: str = Field(min_length=1)
    repo: str = Field(pattern=_REPO)
    pr: int = Field(gt=0)
    head_sha: str = Field(pattern=_SHA)


class ModelCloudLaneRun(BaseModel):
    """What one review-only cloud lane left on its run receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = ""
    status: str = ""
    session_id: str = ""
    session_url: str = ""
    model: str = ""
    review: ModelCloudReview | None = None
    refusal: str = ""


class ModelCloudVoteFinding(BaseModel):
    """One finding the voter raised."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    line: int = Field(ge=0)
    severity: str = Field(min_length=1)
    claim: str = ""


class ModelCloudVote(BaseModel):
    """The unsigned vote: the provenance of the cloud session and the findings it returned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str
    endpoint: str
    model: str
    delegation_event_id: str = Field(min_length=1)
    findings: tuple[ModelCloudVoteFinding, ...] = ()


class ModelCloudRungTopics(BaseModel):
    """The command topic and the delegation terminal topic, as the contract declares them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    delegation_completed: str


class ModelCloudEmitReceipt(BaseModel):
    """What the bus said about one delegation event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = ""
    published: bool = False
    detail: str = ""


__all__ = [
    "EnumCloudRungTransport",
    "ModelCloudEmitReceipt",
    "ModelCloudLaneRun",
    "ModelCloudRungConfig",
    "ModelCloudRungTopics",
    "ModelCloudVote",
    "ModelCloudVoteFinding",
    "ModelCloudVoteRequest",
]
