# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed loader for the delegation regression corpus (OMN-13540).

Reads ``corpus.yaml`` into frozen, validated models. Shared by the Layer-1
unit tests, the Layer-2 live runner, and the corpus-shape test. Adding a
regression case is a data edit in ``corpus.yaml``; this loader is the only code
that needs to understand the schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

CORPUS_PATH = Path(__file__).parent / "corpus.yaml"

# Behavioral enums for the integration `expected` block. STRUCTURE/BEHAVIOR
# only — never an exact-output assertion (LLM output is non-deterministic).
EnumTierBehavior = Literal[
    "free_local_first",
    "escalates_off_local",
    "escalates_to_metered",
    "all_tiers_exhaust",
]
EnumQualityGate = Literal["passes", "fails", "any"]
EnumTerminal = Literal["completed", "failed"]
EnumCost = Literal["zero", "positive", "any"]
EnumEscalation = Literal["none", "occurs", "exhausts"]
EnumTokens = Literal["positive", "any"]


class ModelXfail(BaseModel):
    """A known-broken case: assert the CORRECT expectation but xfail it.

    The assertion text in the corpus still encodes intended behavior; this block
    only marks the case as expected-to-fail today and names the tracking ticket
    so the nightly is actionable, not perpetually-red noise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(..., min_length=1)
    ticket: str = Field(..., pattern=r"^OMN-\d+$")


class ModelExpected(BaseModel):
    """The expected behavioral block for one corpus case.

    Unit rows carry a free-form ``assertion`` / ``cross_cutting`` label (the real
    check lives in the Layer-1 test module). Integration rows carry the typed
    behavioral fields the Layer-2 runner asserts against ``delegation_events``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Unit-row companion labels (documentation; real assertion is in code).
    assertion: str | None = None
    regression_for: str | None = None

    # Integration behavioral fields.
    tier_behavior: EnumTierBehavior | None = None
    quality_gate: EnumQualityGate | None = None
    terminal: EnumTerminal | None = None
    cost: EnumCost | None = None
    escalation: EnumEscalation | None = None
    tokens: EnumTokens | None = None

    # Cross-cutting invariant label (e.g. I9).
    cross_cutting: str | None = None


class ModelCorpusCase(BaseModel):
    """One delegation regression case (one row in corpus.yaml)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1)
    layer: Literal["unit", "integration"]
    task_type: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    acceptance_criteria: tuple[str, ...] = Field(default=())
    expected: ModelExpected
    xfail: ModelXfail | None = None


class ModelCorpus(BaseModel):
    """The full delegation regression corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    corpus_version: str
    ticket: str = Field(..., pattern=r"^OMN-\d+$")
    cases: tuple[ModelCorpusCase, ...]

    def unit_cases(self) -> tuple[ModelCorpusCase, ...]:
        return tuple(c for c in self.cases if c.layer == "unit")

    def integration_cases(self) -> tuple[ModelCorpusCase, ...]:
        return tuple(c for c in self.cases if c.layer == "integration")

    def by_id(self, case_id: str) -> ModelCorpusCase:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise KeyError(f"no corpus case with id={case_id!r}")


def load_corpus(path: Path | None = None) -> ModelCorpus:
    """Load and validate the delegation regression corpus.

    Args:
        path: Optional override path. Defaults to the repo-canonical
            ``tests/delegation_golden/corpus.yaml``.

    Returns:
        A validated, frozen :class:`ModelCorpus`.

    Raises:
        FileNotFoundError: if the corpus file does not exist.
        pydantic.ValidationError: if the corpus does not satisfy the schema.
    """
    resolved = path or CORPUS_PATH
    raw = yaml.safe_load(resolved.read_text())
    return ModelCorpus.model_validate(raw)


__all__ = [
    "CORPUS_PATH",
    "EnumCost",
    "EnumEscalation",
    "EnumQualityGate",
    "EnumTerminal",
    "EnumTierBehavior",
    "EnumTokens",
    "ModelCorpus",
    "ModelCorpusCase",
    "ModelExpected",
    "ModelXfail",
    "load_corpus",
]
