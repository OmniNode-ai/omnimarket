# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed route and event of ``node_ruling_register_effect`` (OMN-24478).

The route is every deployment fact of a read: the hosts whose argv runs directly, the ssh
target each other host maps to, and the prefix a remote argv travels through with one
``{target}`` slot. The event is one row's read as the journal saw it: ``answered`` for an
OK reading (golden), ``refused`` for UNREAD (error). No host, target or topic is in this
file; the overlay and the contract carry them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_ruling_register import EnumReadStatus

#: The prefix slot a remote read's host target is substituted into.
TARGET_TOKEN: Final[str] = "{target}"


class EnumRouteKind(StrEnum):
    """INMEMORY: readings come from a port that holds them. COMMAND: each row's read runs as a command."""

    INMEMORY = "inmemory"
    COMMAND = "command"


class ModelRulingRoute(BaseModel):
    """How a read reaches its host: the direct hosts, the remote targets and the ssh prefix."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumRouteKind
    timeout_seconds: int = Field(ge=1)
    remote_argv_prefix: tuple[str, ...]
    local_hosts: tuple[str, ...]
    host_targets: dict[str, str]
    journal_env: str = Field(min_length=1)

    @model_validator(mode="after")
    def _the_prefix_carries_one_target_slot(self) -> ModelRulingRoute:
        """A remote argv must have exactly one slot to carry its host's target."""
        if self.kind is EnumRouteKind.COMMAND and (
            sum(token.count(TARGET_TOKEN) for token in self.remote_argv_prefix) != 1
        ):
            raise ValueError(
                "a command route's remote_argv_prefix carries {target} exactly once"
            )
        return self


class ModelRulingReadEvent(BaseModel):
    """One row's read as the journal saw it: ``answered`` for OK (golden), ``refused`` for UNREAD (error)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["answered", "refused"]
    topic: str
    row_id: str
    ruling: str
    status: EnumReadStatus
    exit_code: int | None = None
    detail: str = ""
    read_at: AwareDatetime
    correlation_id: str = Field(min_length=1)


__all__ = [
    "TARGET_TOKEN",
    "EnumRouteKind",
    "ModelRulingReadEvent",
    "ModelRulingRoute",
]
