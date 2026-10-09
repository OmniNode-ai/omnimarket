# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Resolved escalation rungs with explicit harness refusal reasons."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_harness_rung_refusal import EnumHarnessRungRefusal


class ModelResolvedChainRung(BaseModel):
    """One declared rung and whether the routing-side checks admit it."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tier: str
    kind: Literal["ladder", "harness"]
    backend_id: str | None
    model_name: str | None
    refusals: tuple[EnumHarnessRungRefusal, ...]

    @property
    def selectable(self) -> bool:
        """A rung is selectable exactly when it has no refusals."""
        return not self.refusals


class ModelResolvedEscalationChain(BaseModel):
    """A task class's acceptance-check chain in declared order."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    task_type: str
    escalate_on: Literal["acceptance_check"]
    rungs: tuple[ModelResolvedChainRung, ...]


__all__: list[str] = ["ModelResolvedChainRung", "ModelResolvedEscalationChain"]
