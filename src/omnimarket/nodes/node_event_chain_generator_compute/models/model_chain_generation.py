# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Walker obligations, generated chain expectations and the gate verdict."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChainKind = Literal["golden", "error", "open"]
TerminalValue = str | int | float | bool | None


class ModelChainObligation(BaseModel):
    """One owner-level walker path, deduplicated: ``(from, trigger, to)`` steps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_id: str
    kind: ChainKind
    steps: tuple[tuple[str, str, str], ...]

    @property
    def triggers(self) -> tuple[str, ...]:
        return tuple(trigger for _, trigger, _ in self.steps)


class ModelDrivenChain(BaseModel):
    """What one drive of a path observed on the bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_types: tuple[str, ...]
    states: tuple[str, ...]
    terminal_payload: dict[str, object]


class ModelGeneratedChain(BaseModel):
    """The golden or error chain expectation for one walker path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_id: str
    kind: ChainKind
    expected_event_types: tuple[str, ...]
    expected_states: tuple[str, ...]
    terminal_fields: dict[str, TerminalValue]


class ModelChainExpectationSet(BaseModel):
    """Every chain of one workflow; the committed form is this model as JSON."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    workflow_owner: str
    chains: tuple[ModelGeneratedChain, ...]
    undriven: dict[str, str] = Field(
        default_factory=dict,
        description="path_id -> why the driver could not drive it (no fixture).",
    )


class ModelChainMismatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path_id: str
    field: str
    committed: object
    generated: object


class ModelChainGateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_owner: str
    obligations: tuple[ModelChainObligation, ...]
    committed: ModelChainExpectationSet | None
    generated: ModelChainExpectationSet


class ModelChainGateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_owner: str
    passed: bool
    missing_chain_path_ids: tuple[str, ...]
    stale_chain_path_ids: tuple[str, ...]
    undriven_path_ids: tuple[str, ...]
    mismatches: tuple[ModelChainMismatch, ...]


__all__ = [
    "ChainKind",
    "ModelChainExpectationSet",
    "ModelChainGateRequest",
    "ModelChainGateResult",
    "ModelChainMismatch",
    "ModelChainObligation",
    "ModelDrivenChain",
    "ModelGeneratedChain",
    "TerminalValue",
]
