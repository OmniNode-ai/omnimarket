# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the tracker's routes reading (OMN-23381): delegations per hour by route, and the local share.

A route is decided by the answering backend's name: a name that starts with the local prefix is local, one that starts with
the GLM prefix is GLM, every other name is other. The prefixes, the window and where the table is read are declared, not coded.
A reading that was not made, cannot be parsed or is older than its declared age has no rows and no share.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# A name that can be placed in a shell command and a SQL literal unquoted: no quote, space or shell character.
SAFE_NAME = r"^[A-Za-z0-9_.:@-]+$"

RoutesState = Literal["fresh", "stale", "unread"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelRoutesSource(_Frozen):
    """The overlay's routes block: where the delegation events are read and which backend names are which route."""

    ssh_host_env: str = Field(
        pattern=SAFE_NAME, description="the variable that holds the ssh target"
    )
    compose_project: str = Field(pattern=SAFE_NAME)
    service: str = Field(pattern=SAFE_NAME, description="the postgres compose service")
    database: str = Field(pattern=SAFE_NAME)
    db_user: str = Field(pattern=SAFE_NAME)
    local_prefix: str = Field(pattern=SAFE_NAME)
    glm_prefix: str = Field(pattern=SAFE_NAME)
    window_hours: int = Field(
        gt=0, le=168, description="how many hours, the current one included, are read"
    )


class ModelRouteCounts(_Frozen):
    """One hour of the read: delegations by route. ``hour`` is the UTC start of the hour."""

    hour: str = Field(min_length=1)
    local: int = Field(ge=0)
    glm: int = Field(ge=0)
    other: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def _routes_add_up(self) -> ModelRouteCounts:
        if self.local + self.glm + self.other != self.total:
            raise ValueError(f"the routes of {self.hour} do not add up to its total")
        return self


class ModelRouteReads(_Frozen):
    """The file the routes stage writes (route-reads.json). A failed read has ``ok`` false, the reason and no hours."""

    read_at: str
    ok: bool
    reason: str = ""
    window_hours: int = Field(gt=0)
    hours: tuple[ModelRouteCounts, ...] = ()


class ModelRouteHour(ModelRouteCounts):
    """One hour of the served block, with its local share; none when the hour had no delegations."""

    local_share: float | None


class ModelRoutesBlock(_Frozen):
    """The ``routes`` block of delegation-tracker.json.

    Only a ``fresh`` block has rows and shares. ``local_share`` is the last hour's.
    """

    state: RoutesState
    reason: str = ""
    read_at: str = ""
    age_minutes: int | None = None
    max_age_min: int = Field(gt=0)
    window_hours: int | None = None
    hours: tuple[ModelRouteHour, ...] = ()
    last_hour: ModelRouteHour | None = None
    local_share: float | None = None
