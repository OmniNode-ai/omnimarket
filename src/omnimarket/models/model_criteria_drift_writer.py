# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The writer of a criteria drift, measured from the ticket's history and the contract."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_criteria_drift_writer import EnumCriteriaDriftWriter


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ModelDescriptionEdit(BaseModel):
    """One entry of a ticket's history that rewrote its description."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    actor: str = Field(..., min_length=1, description="Who made the edit.")
    created_at: datetime = Field(..., description="When the edit was recorded.")


class ModelDescriptionEditsRead(BaseModel):
    """The description edits of one ticket as one history read returned them.

    Exactly one of ``edits`` (possibly empty: a ticket never edited) and
    ``unavailable_reason`` is meaningful, so an unreadable history is never taken
    for a history with no edit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., min_length=1)
    edits: tuple[ModelDescriptionEdit, ...] = Field(default=())
    unavailable_reason: str | None = Field(
        default=None,
        description="Why the history could not be read. Never carries a credential.",
    )

    @model_validator(mode="after")
    def _unavailable_reads_hold_no_edits(self) -> Self:
        if self.unavailable_reason is not None and self.edits:
            raise ValueError(
                f"an unreadable history of {self.ticket_id} cannot carry edits"
            )
        return self


class ModelCriteriaDriftWriter(BaseModel):
    """Who wrote a drift: a ticket edit, the contract, or nobody measurable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    writer: EnumCriteriaDriftWriter
    actor: str | None = Field(
        default=None, description="The editor, for a ticket-edit writer."
    )
    at: datetime | None = Field(
        default=None, description="The edit's time, for a ticket-edit writer."
    )
    contract_path: str | None = Field(
        default=None, description="For a contract writer."
    )
    head_sha: str | None = Field(
        default=None, description="The pull request head carrying the contract."
    )
    reason: str | None = Field(
        default=None, description="Why the writer is unmeasured."
    )

    @model_validator(mode="after")
    def _carries_what_its_label_names(self) -> Self:
        if self.writer is EnumCriteriaDriftWriter.TICKET_EDIT and (
            not self.actor or self.at is None
        ):
            raise ValueError("a ticket-edit writer names the actor and the time")
        if self.writer is EnumCriteriaDriftWriter.CONTRACT and (
            not self.contract_path or not self.head_sha
        ):
            raise ValueError("a contract writer names the contract path and the head")
        if self.writer is EnumCriteriaDriftWriter.UNMEASURED and not self.reason:
            raise ValueError("an unmeasured writer names why")
        return self

    def label(self) -> str:
        """``writer=<kind> <detail>``: the text a drift message starts with."""
        if self.writer is EnumCriteriaDriftWriter.TICKET_EDIT and self.at is not None:
            return f"writer={self.writer.value} {self.actor} {_stamp(self.at)}"
        if self.writer is EnumCriteriaDriftWriter.CONTRACT:
            return f"writer={self.writer.value} {self.contract_path} at {self.head_sha}"
        return f"writer={self.writer.value} ({self.reason})"


__all__ = [
    "ModelCriteriaDriftWriter",
    "ModelDescriptionEdit",
    "ModelDescriptionEditsRead",
]
