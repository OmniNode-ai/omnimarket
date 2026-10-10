# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed retirement entries and their audit resolution (OMN-20157)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.enums.enum_ac_binding_retirement_reason import (
    EnumAcBindingRetirementReason,
)


class ModelAcBindingRetirement(BaseModel):
    """A carrier's explicit, attributed withdrawal of a proposed binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item: str = Field(strict=True, min_length=1)
    label: str = Field(strict=True, min_length=1)
    reason_kind: EnumAcBindingRetirementReason
    superseded_by: str | None = Field(default=None, strict=True)
    reason: str = Field(strict=True)
    retired_by: str = Field(strict=True)
    retired_at: str = Field(strict=True)
    carried_by: str = Field(strict=True, min_length=1)

    @field_validator("item", "label", "superseded_by", "retired_by", "carried_by")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        """Identifiers and attribution must name something."""
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("reason")
    @classmethod
    def explanatory_reason(cls, value: str) -> str:
        """Require an explanation, rather than an empty retirement marker."""
        value = value.strip()
        if len(value) < 12:
            raise ValueError("reason must contain at least 12 characters after strip")
        return value

    @field_validator("retired_at")
    @classmethod
    def utc_second(cls, value: str) -> str:
        """Keep audit timestamps at RFC 3339 UTC second precision."""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is None:
            raise ValueError("retired_at must be RFC 3339 UTC to the second")
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return value

    @model_validator(mode="after")
    def superseder_for_kind(self) -> Self:
        """A superseder is required exactly for the superseded_by reason."""
        if self.reason_kind is EnumAcBindingRetirementReason.SUPERSEDED_BY:
            if self.superseded_by is None:
                raise ValueError("superseded_by is required for this reason_kind")
        elif "superseded_by" in self.model_fields_set:
            raise ValueError("superseded_by must be absent for no_longer_applicable")
        return self


class ModelAcBindingRetirementResolution(BaseModel):
    """Effective withdrawals and refusals, both in contract order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    applied: tuple[ModelAcBindingRetirement, ...] = ()
    refused: tuple[str, ...] = ()

    @property
    def pairs(self) -> frozenset[tuple[str, str]]:
        """Target ids and canonical labels withdrawn by this resolution."""
        return frozenset(
            (entry.item, entry.label.strip().upper().replace("-", "").replace("_", ""))
            for entry in self.applied
        )

    @property
    def applied_summaries(self) -> tuple[str, ...]:
        """Human-readable attribution for receipts and verdicts."""
        return tuple(
            f"{entry.item}:{entry.label} retired_by={entry.retired_by} "
            f"at={entry.retired_at} reason={entry.reason_kind.value}"
            f"{':' + entry.superseded_by if entry.superseded_by is not None else ''} "
            f"({entry.reason})"
            for entry in self.applied
        )
