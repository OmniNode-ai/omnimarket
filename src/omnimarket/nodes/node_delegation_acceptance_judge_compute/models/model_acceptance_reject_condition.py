# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One condition under which the judge rejects, tied to a failure class."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)


class ModelAcceptanceRejectCondition(BaseModel):
    """One condition under which the judge rejects, tied to a failure class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_class: EnumAcceptanceFailureClass
    text: str = Field(min_length=1)
