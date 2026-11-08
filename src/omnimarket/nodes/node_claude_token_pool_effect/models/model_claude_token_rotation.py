# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the rotation tick is asked, reads, decides and records (OMN-23000).

No model here holds a token: a host's login is identified by the label and fingerprint the pool already
shows, and every event names labels only. The overlay holds every setting; nothing has a default.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelRotationMark(BaseModel):
    """One label's marks at the source: a limit that ends at ``until`` and whether its login was revoked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    until: str | None = None
    revoked: bool = False


class ModelRotationMarksRead(BaseModel):
    """What reading the one source gave: marks and the time it wrote them, or why it could not be read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["read", "unreadable"]
    at: datetime | None = None
    marks: tuple[ModelRotationMark, ...] = ()
    detail: str = ""


class ModelRotationHost(BaseModel):
    """A host the tick is declared for. Its place in the overlay's host list is its rank."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user", "launchd"]
    #: The venv whose interpreter runs the installed unit, relative to the registry root or absolute.
    venv: str = Field(min_length=1)
    #: The login the host's lanes read, relative to the user's home.
    token_file: str = Field(min_length=1)


class ModelRotationMarksSource(BaseModel):
    """The one source of limited and revoked marks: a host's pool status mirror, read in place or over ssh."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    #: What ssh dials to reach the source host; unset reads the file on this host.
    ssh_target: str | None
    #: The status mirror the source host's pool writes, relative to the user's home.
    mirror_path: str = Field(min_length=1)
    #: A source older than this is refused, so a dead source host never erases a mark.
    max_age_seconds: int = Field(ge=1)


class ModelRotationSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_seconds: int = Field(ge=60)


class ModelRotationOverlay(BaseModel):
    """``<root>/node_claude_token_pool_effect/overlay.yaml``: the hosts, the source, the events file and the interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[ModelRotationHost, ...] = Field(min_length=1)
    marks_source: ModelRotationMarksSource
    #: Where rotations are appended, relative to the unit's state directory.
    events_file: str = Field(min_length=1)
    unit_environment: dict[str, str]
    schedule: ModelRotationSchedule

    @model_validator(mode="after")
    def _shape(self) -> ModelRotationOverlay:
        names = [host.name for host in self.hosts]
        if len(set(names)) != len(names):
            raise ValueError("overlay hosts name a host twice")
        if self.marks_source.host not in names:
            raise ValueError("marks_source.host is not one of the overlay hosts")
        return self

    def rank(self, name: str) -> int | None:
        return next((i for i, h in enumerate(self.hosts) if h.name == name), None)

    def host(self, name: str) -> ModelRotationHost | None:
        return next((h for h in self.hosts if h.name == name), None)


class ModelClaudeTokenRotationRequest(BaseModel):
    """One tick on ``host``. ``dry_run`` decides and writes nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    dry_run: bool = False


class ModelRotationMarksSync(BaseModel):
    """What re-syncing the pool's marks from the source did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["applied", "unchanged", "unreadable", "stale", "is_source"]
    changed: tuple[str, ...] = ()
    detail: str = ""


class ModelRotationEvent(BaseModel):
    """One line of the rotation record: a host moved off a dead login, or found none to move to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["rotated", "no_usable"]
    host: str
    from_label: str | None
    to_label: str | None
    reason: Literal["limited", "revoked", "absent"]
    at: datetime


class ModelClaudeTokenRotationResult(BaseModel):
    """The tick's decision. Labels and states only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    rank: int
    outcome: Literal["kept", "swapped", "unmanaged", "no_usable", "planned"]
    in_use_state: Literal["usable", "limited", "revoked", "unmanaged", "absent"]
    from_label: str | None = None
    to_label: str | None = None
    order: tuple[str, ...] = ()
    marks_sync: ModelRotationMarksSync
    events: tuple[ModelRotationEvent, ...] = ()
    alarm: str | None = None
    lines: tuple[str, ...] = ()


__all__ = [
    "ModelClaudeTokenRotationRequest",
    "ModelClaudeTokenRotationResult",
    "ModelRotationEvent",
    "ModelRotationHost",
    "ModelRotationMark",
    "ModelRotationMarksRead",
    "ModelRotationMarksSource",
    "ModelRotationMarksSync",
    "ModelRotationOverlay",
    "ModelRotationSchedule",
]
