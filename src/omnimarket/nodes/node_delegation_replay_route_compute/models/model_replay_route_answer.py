# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_replay_route_compute.models.enum_replay_route import (
    EnumReplayAcceptBasis,
    EnumReplayRefusal,
    EnumReplayRoute,
)


class ModelReplayRouteAnswer(BaseModel):
    """How one replay task is sent and how it is scored, or why it is refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    pid: str
    repo_edit: bool
    route: EnumReplayRoute | None = None
    repository: str | None = None
    source_sha: str | None = None
    check_commands: tuple[str, ...] = ()
    accepted: bool | None = None
    accept_basis: EnumReplayAcceptBasis | None = None
    refusal: EnumReplayRefusal | None = None
    message: str = ""


__all__: list[str] = ["ModelReplayRouteAnswer"]
