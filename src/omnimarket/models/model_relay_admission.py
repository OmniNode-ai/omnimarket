# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire seam for the core-owned relay_requires rule; no validation predicate here."""

from pydantic import BaseModel, ConfigDict


class ModelRelayRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_type: str
    cells: dict[str, tuple[str, ...]]


class ModelRelayRequiresRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str
    row_type: str
    trigger_cell: str
    effect_cell: str
    effects: tuple[str, ...]
    target_cell: str
    forbidden_targets: tuple[str, ...]
    target_pattern: str
    fix: str
    evaluator_module: str
    evaluator_function: str


class ModelRelayAdmissionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    refusals: tuple[str, ...] = ()
