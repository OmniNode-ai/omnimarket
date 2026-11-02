# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, target and result of one scoped standalone-service redeploy."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumStandaloneRedeployOutcome(StrEnum):
    PUBLISHED = "published"
    REFUSED = "refused"


class EnumStandaloneRedeployRefusal(StrEnum):
    """Why a request was refused. Nothing is published for any of them."""

    LANE_NOT_ALLOWED = "lane-not-allowed"
    COMPOSE_FILE_NOT_DECLARED = "compose-file-not-declared"
    SERVICE_NOT_DECLARED = "service-not-declared"
    SIGNING_KEY_NOT_SET = "signing-key-not-set"


class ModelStandaloneRedeployTarget(BaseModel):
    """One compose file and service the contract declares the route may touch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    compose_file: str = Field(min_length=1)
    service: str = Field(min_length=1)


class ModelStandaloneServiceRedeployRequest(BaseModel):
    """Recreate one service of one declared standalone compose file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    requested_by: str = Field(min_length=1)
    runtime_lane: str = Field(min_length=1)
    compose_file: str = Field(min_length=1)
    service: str = Field(min_length=1)
    git_ref: str = Field(min_length=1)


class ModelStandaloneServiceRedeployResult(BaseModel):
    """The outcome of one request: published to the deploy agent, or refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    outcome: EnumStandaloneRedeployOutcome
    refusal: EnumStandaloneRedeployRefusal | None = None
    topic: str = ""
    message: str
