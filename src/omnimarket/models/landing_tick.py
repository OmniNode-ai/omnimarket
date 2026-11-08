# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick reducer's input and output, shared with the tick orchestrator: one tick's decision folded to a row per subject."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import ModelLandingDecision

TICK_LEVEL_SUBJECT = "<tick>"


class ModelTickReduceInput(BaseModel):
    """The node path's decision for one tick, and the script's recorded decision when there is one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ModelLandingDecision
    recorded: ModelLandingDecision | None = Field(
        default=None,
        description="The decision the live script recorded for the same tick, or None.",
    )


class ModelTickDifference(BaseModel):
    """One path where the node path differs from the recorded decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(description="JSON path from the subject's document root.")
    recorded: str = Field(description="The recorded value as JSON, or <absent>.")
    node: str = Field(description="The node path's value as JSON, or <absent>.")


class ModelTickSubjectRow(BaseModel):
    """What the node path decided for one subject (a pull request, a cause or the tick itself)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(description="owner/repo#n, a cause key, or <tick>.")
    action_kinds: tuple[str, ...] = Field(
        description="Kinds of the subject's actions, in decision order."
    )
    document: str = Field(
        description="The subject's decision sections as canonical JSON (sorted keys)."
    )


class ModelTickDivergence(BaseModel):
    """One subject where the node path and the recorded decision differ."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str
    differences: tuple[ModelTickDifference, ...]


class ModelTickReduceOutput(BaseModel):
    """The tick folded to rows, and every subject that differs from the recorded decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick: int
    rows: tuple[ModelTickSubjectRow, ...]
    divergences: tuple[ModelTickDivergence, ...] = ()
    compared: bool = Field(description="False when no recorded decision was given.")

    @property
    def equal(self) -> bool:
        return self.compared and not self.divergences


__all__: list[str] = [
    "TICK_LEVEL_SUBJECT",
    "ModelTickDifference",
    "ModelTickDivergence",
    "ModelTickReduceInput",
    "ModelTickReduceOutput",
    "ModelTickSubjectRow",
]
