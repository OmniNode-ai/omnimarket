# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of the lane-brief section 3a render (OMN-21118, plan task TA.17)."""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.lane_brief_section.enum_lane_brief_section_status import (
    EnumLaneBriefSectionStatus,
)


class ModelLaneBriefSectionResult(BaseModel):
    """The rendered section, or the refusal naming each missing factor and cited ruling."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumLaneBriefSectionStatus
    section_3a: str | None = None
    failure_class: EnumContextPackFailure | None = None
    missing_factors: tuple[EnumContextFactor, ...] = ()
    missing_slots: tuple[str, ...] = Field(
        default=(), description="Entry slots a required factor lacked, as `FACTOR:slot`"
    )
    unbacked_ruling_stamps: tuple[str, ...] = ()
    section_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _status_agrees_with_payload(self) -> ModelLaneBriefSectionResult:
        rendered = self.status is EnumLaneBriefSectionStatus.RENDERED
        if rendered and (self.section_3a is None or self.failure_class is not None):
            raise ValueError(
                "a rendered result carries the section and no failure class"
            )
        if not rendered and (self.section_3a is not None or self.failure_class is None):
            raise ValueError("a refused result carries a failure class and no section")
        return self
