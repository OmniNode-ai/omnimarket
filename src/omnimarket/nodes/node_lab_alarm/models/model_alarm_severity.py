# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared severity of every alarm source, and the routing it selects (OMN-24939).

The table is data in node_lab_alarm's contract (``alarm_sources`` and ``alarm_routing``), never code per alarm.
Every source carries exactly one severity; a source with none, a fix-cause source with no cause, a condition listed
twice, or a CLI operation of the node that no source covers refuses the load. The router reads the severity of a
delivery request's condition from it and refuses a condition the table does not declare.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumAlarmSeverity(StrEnum):
    """Where one source's new conditions go."""

    #: Posted to the alert channel once per new condition, and once more when it clears.
    PAGE = "page"
    #: Held for the one daily digest message; its ledger row is still written.
    DIGEST = "digest"
    #: Never posted; its ledger row is still written.
    DROP = "drop"
    #: Routed as PAGE, and listed with its cause in the daily STATUS row so the cause gets an owner.
    FIX_CAUSE = "fix-cause"


class UndeclaredAlarmSourceError(ValueError):
    """A delivery request names a condition the severity table does not declare."""


class ModelAlarmSource(BaseModel):
    """One alarm source: the condition key a delivery request carries, its severity and where it is raised."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The condition name a delivery request carries; for a direct poster, the name the table knows it by.
    condition: str = Field(min_length=1)
    severity: EnumAlarmSeverity
    #: The alarm review's name for the source.
    review_name: str = Field(min_length=1)
    #: ``named``: the review names this source; ``nearest``: the class of the nearest named source;
    #: ``default``: no near source, digest.
    basis: Literal["named", "nearest", "default"]
    #: ``router``: delivered through node_lab_alarm's delivery, so this table routes it.
    #: ``deliver-effect``: delivered through node_lab_alarm_deliver_effect by another node, not through this router.
    #: ``direct``: posts to the channel by its own path; declared here, not routed by it.
    posts_via: Literal["router", "deliver-effect", "direct"]
    #: Where the source is raised, as ``<repo>/<path>:<line>``, or ``not found``.
    raised_at: str = Field(min_length=1)
    #: The node_lab_alarm CLI operation that raises it (router sources only).
    operation: str | None = None
    #: The cause as the review gave it; required for a fix-cause source.
    cause: str = ""

    @model_validator(mode="after")
    def _shape(self) -> ModelAlarmSource:
        if self.severity is EnumAlarmSeverity.FIX_CAUSE and not self.cause.strip():
            raise ValueError(f"fix-cause source {self.condition} names no cause")
        if self.posts_via == "router" and not self.operation:
            raise ValueError(
                f"router source {self.condition} names no node_lab_alarm operation"
            )
        return self


class ModelFixCauseStatusSettings(BaseModel):
    """The STATUS row the daily digest step appends listing the fix-cause sources."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The ticket the row's ``ticket=`` cell carries.
    ticket: str = Field(pattern=r"^OMN-\d+$")
    #: The ``kind=`` cell.
    kind: str = Field(min_length=1)


class ModelAlarmRoutingSettings(BaseModel):
    """How the routes post: the clear message, the digest message and where the digest is held."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Held beside the delivery's episode state (the directory of its ``state_path``).
    digest_state_file: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    #: The category the digest message is posted under.
    digest_category: str = Field(min_length=1)
    #: ``{day}``, ``{episodes}`` and ``{sources}`` are filled in.
    digest_header_template: str = Field(min_length=1)
    #: ``{review_name}``, ``{condition}``, ``{episodes}``, ``{subject}`` and ``{detail}`` are filled in.
    digest_line_template: str = Field(min_length=1)
    #: Most source lines one digest message carries; the rest are counted on one line.
    digest_line_limit: int = Field(ge=1)
    #: Longest detail quoted on one digest line.
    digest_detail_limit: int = Field(ge=16)
    #: The category the clear message of a PAGE or FIX-CAUSE condition is posted under.
    clear_category: str = Field(min_length=1)
    #: ``{condition}`` and ``{subject}`` are filled in.
    clear_template: str = Field(min_length=1)
    fix_cause_status: ModelFixCauseStatusSettings

    @field_validator("digest_header_template")
    @classmethod
    def _header(cls, value: str) -> str:
        for name in ("{day}", "{episodes}", "{sources}"):
            if name not in value:
                raise ValueError(f"the digest header template names no {name}")
        return value

    @field_validator("clear_template")
    @classmethod
    def _clear(cls, value: str) -> str:
        for name in ("{condition}", "{subject}"):
            if name not in value:
                raise ValueError(f"the clear template names no {name}")
        return value


class ModelAlarmSeverityTable(BaseModel):
    """Every declared source, and the routing settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sources: tuple[ModelAlarmSource, ...] = Field(min_length=1)
    routing: ModelAlarmRoutingSettings

    @model_validator(mode="after")
    def _unique(self) -> ModelAlarmSeverityTable:
        names = [s.condition for s in self.sources]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ValueError(f"alarm sources listed twice: {twice}")
        return self

    def source(self, condition: str) -> ModelAlarmSource:
        """The declared source of a condition; an undeclared condition is refused."""
        for declared in self.sources:
            if declared.condition == condition:
                return declared
        raise UndeclaredAlarmSourceError(
            f"alarm condition {condition!r} has no declared severity in node_lab_alarm's contract"
        )

    def of(self, severity: EnumAlarmSeverity) -> tuple[ModelAlarmSource, ...]:
        return tuple(s for s in self.sources if s.severity is severity)


class ModelAlarmDigestCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads what is due and sends nothing, keeps nothing (a probe).
    write: bool = True


class EnumAlarmDigestOutcome(StrEnum):
    #: One digest message per due day was posted (and the STATUS row appended or already held).
    SENT = "sent"
    #: No held day was due; the STATUS row was still appended or already held.
    NOTHING_DUE = "nothing-due"
    #: A digest post or the STATUS row append failed; the day stays held and is retried next run.
    FAILED = "failed"
    #: A probe: nothing sent, nothing kept.
    SKIPPED = "skipped"


class ModelAlarmDigestDay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    day: str
    episodes: int = Field(ge=0)
    sources: int = Field(ge=0)
    message_ref: str = ""
    failure: str = ""


class ModelAlarmDigestResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumAlarmDigestOutcome
    days: tuple[ModelAlarmDigestDay, ...]
    #: The STATUS row's step: ``accepted``, ``duplicate``, ``failed`` or ``not-run``.
    status_row: str
    status_request_id: str = ""
    read_at: datetime
    failure: str = ""


__all__ = [
    "EnumAlarmDigestOutcome",
    "EnumAlarmSeverity",
    "ModelAlarmDigestCommand",
    "ModelAlarmDigestDay",
    "ModelAlarmDigestResult",
    "ModelAlarmRoutingSettings",
    "ModelAlarmSeverityTable",
    "ModelAlarmSource",
    "ModelFixCauseStatusSettings",
    "UndeclaredAlarmSourceError",
]
