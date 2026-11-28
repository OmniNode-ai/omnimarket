# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alert channel's post policy: one declared overlay, one decision per message (OMN-24562).

A post is for a person who must act. Every value here (the kinds, their class, dedup key, reminder interval and
resolve rule, the self-healing and unreadable patterns, the hours) is the overlay's; nothing has a default in
code except the shape.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EnumAlarmClass(StrEnum):
    #: A person must act: posted once per condition, reminded on the interval, resolved once.
    PAGE = "page"
    #: A reading: goes to the bus, never to the channel.
    QUIET = "quiet"


class EnumPostAction(StrEnum):
    POST = "post"
    REMINDER = "reminder"
    RESOLVED = "resolved"
    #: Over the ticket age with no owner: a ticket is raised and the condition stops posting.
    TICKET = "ticket"
    #: A detector that could not read for longer than the overlay's hours: one post about itself.
    DETECTOR_DOWN = "detector-down"
    SUPPRESS = "suppress"


class ModelAlarmKindPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    #: Matches the message text. A named group ``subject`` names what the condition is about; none means lab-wide.
    match: str = Field(min_length=1)
    alarm_class: EnumAlarmClass
    #: ``{kind}`` and ``{subject}`` are filled in. A lab-wide kind names no ``{subject}`` and no host.
    dedup_key: str = Field(min_length=1)
    #: Hours between reminders while the condition stays open and unchanged.
    reminder_hours: int = Field(ge=1)
    #: Whether a cleared condition posts one resolved message.
    post_resolved: bool
    #: The named action a person takes; a page without one is refused at load.
    action: str = ""
    #: A message matching any of these is self-healing: it never posts (it goes to the bus as a reading).
    self_healing: tuple[str, ...] = ()
    #: A message matching any of these is the detector failing to read: it never posts as the condition.
    unreadable: tuple[str, ...] = ()
    #: A named group ``minutes`` in this pattern gives how long the message says the episode has been open.
    open_minutes: str = ""

    @field_validator("match", "open_minutes")
    @classmethod
    def _compiles(cls, value: str) -> str:
        if value:
            re.compile(value)
        return value

    @field_validator("self_healing", "unreadable")
    @classmethod
    def _each_compiles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            re.compile(pattern)
        return value

    def model_post_init(self, __context: object) -> None:
        if self.alarm_class is EnumAlarmClass.PAGE and not self.action.strip():
            raise ValueError(f"page kind {self.kind} names no action")


class ModelAlarmPostPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kinds: tuple[ModelAlarmKindPolicy, ...]
    #: A message no kind matches is held to this class (page keeps an unknown failure visible).
    unmatched_class: EnumAlarmClass
    unmatched_reminder_hours: int = Field(ge=1)
    #: A detector that read nothing for this many hours posts once about itself.
    unreadable_post_after_hours: int = Field(ge=1)
    #: An episode open this many hours with no owner becomes a ticket and stops posting.
    ticket_after_hours: int = Field(ge=1)
    #: ``{kind}``, ``{subject}``, ``{action}`` and ``{hours}`` are filled in.
    resolved_template: str = Field(min_length=1)
    detector_down_template: str = Field(min_length=1)
    #: argv of the ticket command; ``{kind}``, ``{subject}`` and ``{text}`` are filled in.
    ticket_command: tuple[str, ...] = Field(min_length=1)

    @field_validator("kinds")
    @classmethod
    def _unique(
        cls, value: tuple[ModelAlarmKindPolicy, ...]
    ) -> tuple[ModelAlarmKindPolicy, ...]:
        names = [k.kind for k in value]
        if len(names) != len(set(names)):
            raise ValueError("a kind is declared twice")
        return value


class ModelPostDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: EnumPostAction
    kind: str
    key: str
    #: Why: ``new``, ``changed``, ``reminder``, ``duplicate``, ``self-healing``, ``unreadable``, ``quiet``,
    #: ``ticketed``, ``cleared``.
    reason: str
    #: The text to post when the action posts; empty otherwise.
    text: str = ""


__all__ = [
    "EnumAlarmClass",
    "EnumPostAction",
    "ModelAlarmKindPolicy",
    "ModelAlarmPostPolicy",
    "ModelPostDecision",
]
