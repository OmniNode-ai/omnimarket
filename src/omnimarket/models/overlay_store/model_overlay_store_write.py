# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A write to the settings store: the record a set sends, its audit row, the store's reply and the provenance it keeps.

A set is a whole record of the overlay built on one revision of the store (``based_on_revision``), carrying the path an
operator set (``operator_paths``) and the audit row of the set. The store applies it only when that revision is the
current one, in one transaction with its provenance and its audit row, and answers with the write id it was sent under.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from omnimarket.models.overlay_store.model_overlay_store import (
    ModelOverlayStoreRecord,
    OverlayId,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumOverlayValueSource(StrEnum):
    """Where the value at a path came from. A path with no entry holds the default of the record's ``source_ref``."""

    OPERATOR = "operator"


class EnumOverlayWriteOutcome(StrEnum):
    COMMITTED = "committed"
    CONFLICT = "conflict"


class ModelOverlaySettingAudit(_Frozen):
    """Only declared settings are settable, and a declared setting holds no secret, so the values ride in the row:
    the previous value is what a restore sets back."""

    overlay_id: OverlayId
    key: str
    setting_path: str
    actor: str
    previous_present: bool
    previous_value: JsonValue
    new_value: JsonValue
    document_digest: str
    set_at: datetime


class ModelOverlayStoreWrite(ModelOverlayStoreRecord):
    """A load (no write id) or a set (a write id, the revision it was built on and its audit row)."""

    write_id: str | None = Field(default=None, min_length=1)
    based_on_revision: int | None = Field(default=None, ge=0)
    audit: ModelOverlaySettingAudit | None = None

    @model_validator(mode="after")
    def _a_set_is_whole(self) -> ModelOverlayStoreWrite:
        if self.operator_paths and self.audit is None:
            raise ValueError(
                f"overlay {self.overlay_id}: operator paths without an audit row"
            )
        if self.audit is None:
            return self
        if self.write_id is None:
            raise ValueError(
                f"overlay {self.overlay_id}: an audit row without a write id"
            )
        if self.based_on_revision is None:
            raise ValueError(
                f"overlay {self.overlay_id}: an audit row without the revision it was built on"
            )
        if self.audit.overlay_id != self.overlay_id:
            raise ValueError(
                f"overlay {self.overlay_id}: the audit row names overlay {self.audit.overlay_id}"
            )
        return self


class ModelOverlayWriteReply(_Frozen):
    """The store's answer to one write: committed at ``revision``, or a conflict with the revision the store holds.

    ``replayed`` is true when the write id had been applied before: the answer repeats the first one and nothing was
    written again."""

    write_id: str | None = None
    overlay_id: OverlayId
    outcome: EnumOverlayWriteOutcome
    revision: int = Field(ge=0)
    replayed: bool = False


class ModelOverlayPathProvenance(_Frozen):
    """Who set the value at one path, in which revision, under which write id."""

    source: EnumOverlayValueSource
    actor: str
    revision: int = Field(ge=1)
    write_id: str


__all__ = [
    "EnumOverlayValueSource",
    "EnumOverlayWriteOutcome",
    "ModelOverlayPathProvenance",
    "ModelOverlaySettingAudit",
    "ModelOverlayStoreWrite",
    "ModelOverlayWriteReply",
]
