# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Acceptance-check escalation chains declared by task-class contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelClassEscalationChain(BaseModel):
    """An ordered, duplicate-free chain of ladder and harness tier names."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    escalate_on: Literal["acceptance_check"]
    rungs: tuple[str, ...] = Field(..., min_length=1)

    @field_validator("rungs")
    @classmethod
    def _unique_rungs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            msg = f"escalation_chain.rungs must not contain duplicates, got {value}"
            raise ValueError(msg)
        return value


__all__: list[str] = ["ModelClassEscalationChain"]
