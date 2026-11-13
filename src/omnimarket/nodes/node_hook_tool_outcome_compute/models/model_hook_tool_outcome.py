# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.nodes.node_hook_tool_outcome_compute.models.enum_tool_outcome import (
    EnumToolOutcome,
)


class ModelRefusalRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    pattern: str
    component: str | None = None
    tool_names: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_rule(self) -> ModelRefusalRule:
        if not self.name:
            raise ValueError("name must be non-empty")
        try:
            compiled = re.compile(self.pattern, re.MULTILINE)
        except re.error as exc:
            raise ValueError(f"pattern does not compile: {exc}") from None
        if compiled.groupindex.get("component") is None and self.component is None:
            raise ValueError("rule must have a component group or a fixed component")
        return self


class ModelToolOutcomeRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    refusal_rules: tuple[ModelRefusalRule, ...]
    timeout_markers: tuple[str, ...]
    interrupted_is_timeout: bool
    timeout_exit_codes: tuple[int, ...]
    exit_code_pattern: str

    @model_validator(mode="after")
    def _validate_exit_code_pattern(self) -> ModelToolOutcomeRules:
        try:
            compiled = re.compile(self.exit_code_pattern)
        except re.error as exc:
            raise ValueError(f"exit_code_pattern does not compile: {exc}") from None
        if compiled.groups < 1:
            raise ValueError("exit_code_pattern must have one group")
        return self


class ModelHookToolOutcomeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    hook_event_name: Literal["PostToolUse", "PostToolUseFailure"]
    tool_response: str | None = None
    error: str | None = None
    exit_code: int | None = None
    interrupted: bool | None = None


class ModelHookToolOutcomeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumToolOutcome
    refusing_component: str | None
    matched_rule: str | None
    exit_code: int | None
    reason: str
