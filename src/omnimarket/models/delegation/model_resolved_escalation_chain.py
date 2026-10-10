# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Resolved escalation rungs with explicit harness refusal reasons."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation.model_resolved_chain_rung import (
    ModelResolvedChainRung,
)


class ModelResolvedEscalationChain(BaseModel):
    """A task class's acceptance-check chain in declared order."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    task_type: str
    escalate_on: Literal["acceptance_check"]
    rungs: tuple[ModelResolvedChainRung, ...]


__all__: list[str] = ["ModelResolvedEscalationChain"]
