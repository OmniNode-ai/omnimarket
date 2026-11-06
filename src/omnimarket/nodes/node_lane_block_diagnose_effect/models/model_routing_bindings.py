# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment bindings for the existing admission, fill, item, ticket and triage nodes."""

from pydantic import Field

from omnimarket.models.model_lane_routing import (
    ModelLaneRoutingOverlay,
    ModelRoutingValue,
)


class ModelRoutingEndpoint(ModelRoutingValue):
    node: str = Field(min_length=1)
    command_topic: str = Field(min_length=1)
    terminal_topic: str = Field(min_length=1)


class ModelRoutingBindings(ModelRoutingValue):
    diagnosis: ModelRoutingEndpoint
    admission: ModelRoutingEndpoint
    fill: ModelRoutingEndpoint
    bind: ModelRoutingEndpoint
    exception: ModelRoutingEndpoint
    ticket: ModelRoutingEndpoint
    triage: ModelRoutingEndpoint
    timeout_seconds: int = Field(ge=1)
    exception_state_dir: str = Field(min_length=1)


class ModelLaneRoutingDeployment(ModelLaneRoutingOverlay):
    bindings: ModelRoutingBindings


class ModelRoutingAdmissionReply(ModelRoutingValue):
    allowed: bool
    reason: str


class ModelRoutingLaneReply(ModelRoutingValue):
    lane: str = Field(min_length=1)


class ModelRoutingBindReply(ModelRoutingValue):
    item_ids: tuple[str, ...]


class ModelRoutingTicketReply(ModelRoutingValue):
    repository: str | None
