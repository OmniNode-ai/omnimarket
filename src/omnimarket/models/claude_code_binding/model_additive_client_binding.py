# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An additive client binding row: a profile, a latency budget and a start scope (OMN-22016, TC.11)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from omnibase_core.models.claude_code_binding import ModelClientBinding
from pydantic import Field, model_validator

from omnimarket.models.claude_code_binding.enum_binding_kind import EnumBindingKind

#: The most each additive profile may wait for its package, at p95 (decision D8).
ADDITIVE_BUDGET_CAPS_MS: Mapping[str, int] = {
    "session_start": 5000,
    "agent_spawn": 3000,
    "per_prompt": 500,
}


#: The most text an additive row injects; the capability probe (P19) measured byte-identical delivery
#: to that size, and a larger block is moved to a file, so it is never injected.
ADDITIVE_TEXT_CAP_CHARS = 10_000


class EnumAdditiveStartScope(StrEnum):
    """Where a session must start for the row to act (decision D7)."""

    ANY = "any"
    #: Inside the workspace registry or an internal-library clone; silent everywhere else.
    WORKSPACE = "workspace"


class ModelAdditiveClientBinding(ModelClientBinding):
    """An additive row waits for its package no longer than ``latency_budget_ms``.

    ``deadline_ms`` equals the budget: the budget is the whole of the hook's wait.
    """

    kind: Literal[EnumBindingKind.ADDITIVE] = EnumBindingKind.ADDITIVE
    profile: str = Field(description="Context profile whose package the row injects.")
    latency_budget_ms: int = Field(
        gt=0, description="The wait on the lab bus for the package, in ms."
    )
    start_scope: EnumAdditiveStartScope = Field(default=EnumAdditiveStartScope.ANY)

    @model_validator(mode="after")
    def _within_the_profile_cap(self) -> ModelAdditiveClientBinding:
        cap = ADDITIVE_BUDGET_CAPS_MS.get(self.profile)
        if cap is None:
            raise ValueError(
                f"additive profile {self.profile!r} is not one of "
                f"{sorted(ADDITIVE_BUDGET_CAPS_MS)}"
            )
        if self.latency_budget_ms > cap:
            raise ValueError(
                f"latency_budget_ms {self.latency_budget_ms} is above the {cap} cap "
                f"of profile {self.profile!r}"
            )
        if self.deadline_ms != self.latency_budget_ms:
            raise ValueError(
                "an additive row's deadline_ms equals its latency_budget_ms"
            )
        return self
