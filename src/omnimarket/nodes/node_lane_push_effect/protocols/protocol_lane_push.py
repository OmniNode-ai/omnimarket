# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected dependencies of the lane push handler: every effect is behind a port."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_lane_push_effect.models import (
    ModelLanePushAttempt,
    ModelLanePushCheckVerdict,
    ModelLanePushRequest,
    ModelLanePushSource,
    ModelLaneRulingState,
)


class CommandOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    exit_code: int
    output: str = ""


class ProtocolRemoteHeadReader(Protocol):
    def read(self, repository: str, branch: str) -> str:
        """The branch's remote head, or the all-zero head when it does not exist."""
        ...


class ProtocolSourceMaterializer(Protocol):
    def materialize(
        self, source: ModelLanePushSource, request: ModelLanePushRequest
    ) -> str:
        """The path of a tree checked out at ``source.head_sha``."""
        ...


class ProtocolLanePushCheck(Protocol):
    check_id: str

    def run(
        self, request: ModelLanePushRequest, materialized: str
    ) -> ModelLanePushCheckVerdict: ...


class ProtocolCredentialIssuer(Protocol):
    def issue(self, repository: str, correlation_id: UUID) -> object:
        """A push credential for exactly this repository; never placed in a result."""
        ...


class ProtocolPusher(Protocol):
    # No ``force`` parameter: a plain push is the only push there is.
    def push(
        self, repository: str, branch: str, commit_sha: str, credential: object
    ) -> ModelLanePushAttempt: ...


class ProtocolRulingReader(Protocol):
    def read(self, lane: str) -> ModelLaneRulingState:
        """The lane's CLAIM and later rulings and acknowledgements, read from the bus."""
        ...


class ProtocolCommandRunner(Protocol):
    def run(
        self, argv: Sequence[str], cwd: str, timeout_s: float
    ) -> CommandOutcome: ...


__all__ = [
    "CommandOutcome",
    "ProtocolCommandRunner",
    "ProtocolCredentialIssuer",
    "ProtocolLanePushCheck",
    "ProtocolPusher",
    "ProtocolRemoteHeadReader",
    "ProtocolRulingReader",
    "ProtocolSourceMaterializer",
]
