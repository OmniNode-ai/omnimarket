# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The history of one change (a pull request) as the change source reports it.

A change source returns what happened, in order: who applied or removed which
label, what kind of account did it, and when each head commit was pushed. A source
that can only report the present labels has no history and cannot be used to read a
grant (OMN-21493).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class EnumChangeEventKind(StrEnum):
    LABELED = "labeled"
    UNLABELED = "unlabeled"
    PUSHED = "pushed"


class EnumAccountType(StrEnum):
    """What kind of account performed an event. Only ``user`` is a person."""

    USER = "user"
    BOT = "bot"
    APP = "app"
    ORGANIZATION = "organization"
    MANNEQUIN = "mannequin"
    UNKNOWN = "unknown"


class ModelChangeEvent(BaseModel):
    """One event of a change: a label applied, a label removed or a head pushed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    kind: EnumChangeEventKind
    at: AwareDatetime
    actor_login: str = Field(min_length=1)
    actor_type: EnumAccountType
    via_app: bool = False
    label: str | None = None
    head_sha: str | None = None

    @model_validator(mode="after")
    def _kind_carries_its_subject(self) -> Self:
        if self.kind is EnumChangeEventKind.PUSHED:
            if not self.head_sha:
                raise ValueError("a pushed event carries the head sha it pushed")
        elif not self.label:
            raise ValueError(f"a {self.kind.value} event carries its label")
        return self


class ModelChangeHistory(BaseModel):
    """The events of one change, in the order the source reports them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    pull_request: int = Field(ge=1)
    events: tuple[ModelChangeEvent, ...] = ()
