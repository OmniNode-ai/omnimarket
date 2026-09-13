# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed records for the delegation task-complexity ladder (OMN-18300).

Every field here exists because the report needs it. A run that cannot fill a
field records the reason rather than a default: an absent measurement and a
zero measurement are different facts, and the ladder exists to tell them apart.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRung(StrEnum):
    """The six rungs, in increasing order of what the model must actually do."""

    R1_SINGLE_FACT_REWRITE = "R1"
    R2_MULTI_SOURCE_SUMMARY = "R2"
    R3_CODE_READING_QA = "R3"
    R3B_WORKFLOW_SELECTION = "R3b"
    R4_WRITE_UNIT_TEST = "R4"
    R5_DIAGNOSE_FAILURE = "R5"
    R6_PRODUCE_PATCH = "R6"


class EnumPath(StrEnum):
    """Which delegation surface answered."""

    LOCAL = "local"
    CLOUD = "cloud"


class EnumScorerKind(StrEnum):
    """How a rung is graded.

    ``GROUNDING_RUBRIC`` and ``GROUNDING_COVERAGE`` are mechanical over the
    fed text. ``EXACT_MATCH`` is mechanical against a held answer. The last
    three EXECUTE something and are mechanical in the strongest sense: they
    are not opinions about the output, they are what happened when it ran.
    """

    GROUNDING_RUBRIC = "grounding_rubric"
    GROUNDING_COVERAGE = "grounding_coverage"
    EXACT_MATCH = "exact_match"
    UNIT_TEST_EXECUTION = "unit_test_execution"
    DIAGNOSIS_LOCATION = "diagnosis_location"
    PATCH_APPLY_AND_TEST = "patch_apply_and_test"


class EnumOutcome(StrEnum):
    """Terminal disposition of one delegated task on one path.

    ``REFUSED`` is a first-class result, not an error: a surface that will not
    accept a bundle has told us something about that surface, and shrinking the
    bundle to get a number would erase the finding.
    """

    PASS = "pass"
    FAIL = "fail"
    REFUSED = "refused"
    HARNESS_ERROR = "harness_error"


class ModelTaskBundle(BaseModel):
    """One benchmark task: the exact text fed, and the held answer never fed.

    ``prompt`` is the complete delegated input, byte for byte. Nothing is
    templated at run time, so a rerun months later feeds the identical bytes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    rung: EnumRung
    title: str
    task_type: str = Field(
        description="Passed to the delegate CLI as --task-type. Chosen to match "
        "the work, because the quality gate applies task-type-specific criteria "
        "and a mismatched type produces a gate rejection that says nothing about "
        "the model."
    )
    provenance: str = Field(
        description="Where this task came from in the real workspace: repo, path, "
        "commit, or ledger date. A benchmark task with no provenance is a "
        "synthetic task wearing a costume."
    )
    prompt: str
    scorer: EnumScorerKind
    scorer_config: dict[str, object] = Field(default_factory=dict)
    dependent_reasoning_steps: int = Field(
        default=1,
        ge=1,
        description="Declared per task under the counting rule in "
        "contracts/task_complexity_rubric.v1.yaml. The one feature of the "
        "complexity rubric that is not measured from the fed text.",
    )
    fixture_files: dict[str, str] = Field(
        default_factory=dict,
        description="Fixture basenames this task's scorer materialises, mapped to "
        "their role (subject, mutant, test, focused_test).",
    )


class ModelScore(BaseModel):
    """What the scorer concluded, and the evidence for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumOutcome
    score: float = Field(ge=0.0, le=1.0)
    mechanical: bool = Field(
        description="True when the verdict came from executing something or from "
        "a string comparison against a held answer; False is not permitted by any "
        "scorer in this harness and exists only to make the claim explicit."
    )
    detail: str
    sub_scores: dict[str, float] = Field(default_factory=dict)
    checklist: dict[str, bool] = Field(default_factory=dict)


class ModelRunRecord(BaseModel):
    """One task, one path, one run. Never retried."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    rung: EnumRung
    path: EnumPath
    delegation_id: str | None
    tier: str | None
    backend_id: str | None
    model_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None = Field(
        default=None,
        description="Present on both paths. The gateway reports only a total, so "
        "the split is None there rather than a guess.",
    )
    cost_usd: float | None
    model_latency_ms: int | None = Field(
        default=None,
        description="Provider-side latency from the receipt. Comparable across "
        "runs; unlike wall_ms it excludes client boot.",
    )
    wall_ms: int = Field(
        description="Measured around the whole client invocation, so it carries "
        "CLI startup. Reported separately from model_latency_ms because the two "
        "differ by an order of magnitude on the local path."
    )
    quality_gate_passed: bool | None
    quality_score: float | None
    leak_fraction: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of the response that is reasoning preamble before "
        "the answer proper (OMN-18278). 0.0 means a clean response.",
    )
    response_chars: int
    score: ModelScore
    response_sha256: str = Field(
        description="Of the raw response, so the scored bytes are pinned without "
        "committing every transcript."
    )
    derived_rung: str | None = Field(
        default=None,
        description="The rung the complexity rubric derives from this task's own "
        "bundle, independent of the rung it was filed under. Recorded on every row "
        "so measurement and routing can be read against one classifier.",
    )
    complexity_score: int | None = None
    complexity_features: dict[str, object] = Field(default_factory=dict)
    notes: str = ""
