# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input, overlay policy, and output of the Codex model choice."""

from __future__ import annotations

import re

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from omnimarket.handlers.rules_lane_requirements import declarations

_SECURITY_SCOPE = re.compile(r"Security scope:[ \t]*(yes|no)[ \t]*", re.IGNORECASE)
_ROUND = re.compile(r"Round:[ \t]*([0-9]+)[ \t]*", re.IGNORECASE)


class ModelCodexModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # A missing or blank id asks the overlay policy to choose.
    codex_model: str | None = None
    security_scope: bool = Field(default=False, strict=True)
    round_count: int = Field(default=1, ge=1, strict=True)
    brief: str = ""

    @model_validator(mode="before")
    @classmethod
    def read_brief_metadata(cls, data: object) -> object:
        """Fill omitted fields from the dispatcher's first declaration outside fences."""
        if not isinstance(data, dict) or not isinstance(
            brief := data.get("brief"), str
        ):
            return data
        facts = dict(data)
        for line in declarations(brief):
            if scope := _SECURITY_SCOPE.fullmatch(line):
                facts.setdefault("security_scope", scope[1].lower() == "yes")
            elif "round_count" not in facts and (
                round_number := _ROUND.fullmatch(line)
            ):
                facts["round_count"] = int(round_number[1])
        return facts


class ModelCodexModelDeclaration(BaseModel):
    """Declared Codex models and the overlay's default and escalation policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    codex_models: tuple[str, ...] = Field(min_length=1)
    initial_model: str = Field(min_length=1)
    escalation_model: str = Field(min_length=1)
    escalation_round_threshold: int = Field(ge=1, strict=True)

    @field_validator("initial_model", "escalation_model")
    @classmethod
    def model_is_declared(cls, model: str, info: ValidationInfo) -> str:
        declared = info.data.get("codex_models")
        if declared is not None and model not in declared:
            raise ValueError("model must be listed in codex_models")
        return model


class ModelCodexModelResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # False only for an id the declaration does not list
    accepted: bool
    # The id to pass as ``-m``, or None for a refusal.
    model: str | None = None
    reason: str
    declared: tuple[str, ...]
