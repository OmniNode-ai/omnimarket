# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``lane_brief_pr_gate`` overlay: the gate's mode and the citation that moved it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from omnimarket.models.lane_brief_pr_gate.enum_lane_brief_pr_gate_mode import (
    EnumLaneBriefPrGateMode,
)


class ModelLaneBriefPrGateConfig(BaseModel):
    """``enforce`` needs a ``citation``: the gate's run over the live producers' briefs that justified it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EnumLaneBriefPrGateMode
    citation: str = Field(default="", validate_default=True)

    @field_validator("citation")
    @classmethod
    def _enforce_cites_its_run(cls, value: str, info: ValidationInfo) -> str:
        if (
            info.data.get("mode") is EnumLaneBriefPrGateMode.ENFORCE
            and not value.strip()
        ):
            raise ValueError(
                "lane_brief_pr_gate.citation is empty: enforce moves only on a citation "
                "of the gate's run over the live producers' briefs"
            )
        return value
