# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, finding and result of the trigger declaration check."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumTriggerFindingReason(StrEnum):
    """Why a node that needs a trigger is refused."""

    NO_TRIGGER_DECLARED = "no-trigger-declared"
    INVALID_DECLARATION = "invalid-declaration"
    UNREADABLE_CONTRACT = "unreadable-contract"


class ModelTriggerCheckRequest(BaseModel):
    """Which node contracts to read under the repository root.

    ``contracts`` are repository-relative paths of changed ``contract.yaml`` files; ``all_nodes`` reads every
    node contract of the repository instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: str = Field(min_length=1)
    contracts: tuple[str, ...] = ()
    all_nodes: bool = False


class ModelTriggerFinding(BaseModel):
    """One refused node: its name, its contract path, the reason and what was wrong."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    contract: str
    reason: EnumTriggerFindingReason
    detail: str


class ModelTriggerCheckResult(BaseModel):
    """Every contract read, how many needed a trigger and each refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked: int
    needing_trigger: int
    findings: tuple[ModelTriggerFinding, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.findings
