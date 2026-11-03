# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input, result and policy for the stand-up retrospective node."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnumStandupRetrospectiveTransport(StrEnum):
    """The bus binding the section travels over; the section itself is the same on both."""

    IN_MEMORY = "in_memory"
    REDPANDA = "redpanda"


class ModelStandupRetrospectiveTopics(BaseModel):
    """The command topic a run is requested on and the topic the built section is published on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    built: str


class ModelStandupRetrospectiveRequest(BaseModel):
    """The window's end (ISO 8601) and the text of the ``--retro`` entries file, None when the flag was not given.

    The caller reads the file; an unreadable file is the caller's ``retrospective:`` error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    until: str
    entries: str | None = None


class ModelStandupRetrospectiveResult(BaseModel):
    """``retrospective`` is empty on any day but Friday; ``errors`` carry the ``retrospective:`` source prefix.

    ``section_lines`` is the text rendering of the section and its ticket drafts, empty when there is no section.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    retrospective: dict[str, Any] = Field(default_factory=dict)
    ticket_drafts: list[dict[str, Any]] = Field(default_factory=list)
    section_lines: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ModelStandupRetrospectiveMessages(BaseModel):
    """Every note and error sentence; ``{number}`` and ``{detail}`` are the only slots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    error_prefix: str
    not_a_retro_day_note: str
    no_input_note: str
    not_a_list_detail: str
    no_role_detail: str
    unknown_role_detail: str
    unparsable_detail: str


class ModelStandupRetrospectiveLabels(BaseModel):
    """The text sheet's wording; slots are named in each template."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    section_heading: str
    drafts_heading: str
    person_line: str
    part_separator: str
    use_part: str
    detail_part: str
    change_part: str
    default_kind_label: str
    not_reported: str
    draft_line: str


class ModelStandupRetrospectivePolicy(BaseModel):
    """The contract's ``config.standup_retrospective`` with the overlay laid over it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: EnumStandupRetrospectiveTransport
    retro_weekday: int = Field(ge=0, le=6)
    role_vocabulary: tuple[str, ...] = Field(min_length=1)
    role_key: str
    product_use_key: str
    idea_key: str
    friction_key: str
    process_change_key: str
    idea_kind: str
    friction_kind: str
    draft_state: str
    draft_title_max_chars: int = Field(ge=1)
    credit_template: str
    draft_description_template: str
    messages: ModelStandupRetrospectiveMessages
    labels: ModelStandupRetrospectiveLabels
