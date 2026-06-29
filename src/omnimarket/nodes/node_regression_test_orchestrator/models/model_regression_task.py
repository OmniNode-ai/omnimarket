# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression task definition with provenance (absorbed from SEA ``regression/tasks.py``).

Each task records baseline pass rate and attempt counts so a replay run can be
scored deterministically against the recorded model behavior. Derived from the
OMN-11241 context-matrix experiments. The canonical seven-task suite lives in
:data:`REGRESSION_TASKS`.
"""

from __future__ import annotations

from enum import StrEnum, unique

from pydantic import BaseModel, ConfigDict, Field


@unique
class EnumRegressionDifficulty(StrEnum):
    """Expected difficulty of a regression task (was a free-text str in SEA)."""

    SIMPLE = "simple"
    NORMAL = "normal"
    TOPOLOGY_AFFECTING = "topology_affecting"


class ModelRegressionTask(BaseModel):
    """A single regression task with full baseline provenance.

    Frozen + ``extra="forbid"`` — the suite is a fixed, declared corpus, never
    mutated at runtime.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    task_id: str = Field(
        ..., min_length=1, description="Stable task identifier (e.g. 'reg-001')."
    )
    description: str = Field(..., min_length=1, description="Generation task prompt.")
    expected_difficulty: EnumRegressionDifficulty = Field(
        ..., description="Difficulty class of the task."
    )
    baseline_pass_rate: float = Field(
        ..., ge=0.0, le=1.0, description="Recorded baseline pass rate (0.0-1.0)."
    )
    baseline_avg_attempts: float = Field(
        ..., ge=0.0, description="Recorded baseline average attempt count."
    )
    source_experiment_id: str = Field(
        ..., min_length=1, description="Originating experiment identifier."
    )
    source_ticket_id: str = Field(
        ..., min_length=1, description="Originating Linear ticket identifier."
    )
    baseline_source_path: str = Field(
        ...,
        min_length=1,
        description="Repo-relative path the baseline was captured from.",
    )
    baseline_captured_at: str = Field(
        ..., min_length=1, description="ISO-8601 timestamp the baseline was captured."
    )


REGRESSION_TASKS: tuple[ModelRegressionTask, ...] = (
    ModelRegressionTask(
        task_id="reg-001",
        description="Generate a compute node that converts Celsius to Fahrenheit",
        expected_difficulty=EnumRegressionDifficulty.SIMPLE,
        baseline_pass_rate=1.0,
        baseline_avg_attempts=1.0,
        source_experiment_id="OMN-11241-ctx-matrix",
        source_ticket_id="OMN-11241",
        baseline_source_path=".onex_state/hackathon/context_matrix/baseline.json",
        baseline_captured_at="2026-05-20T00:00:00Z",
    ),
    ModelRegressionTask(
        task_id="reg-002",
        description="Generate a compute node that reverses a string input",
        expected_difficulty=EnumRegressionDifficulty.SIMPLE,
        baseline_pass_rate=1.0,
        baseline_avg_attempts=1.0,
        source_experiment_id="OMN-11241-ctx-matrix",
        source_ticket_id="OMN-11241",
        baseline_source_path=".onex_state/hackathon/context_matrix/baseline.json",
        baseline_captured_at="2026-05-20T00:00:00Z",
    ),
    ModelRegressionTask(
        task_id="reg-003",
        description="Generate a compute node that deduplicates a list while preserving order",
        expected_difficulty=EnumRegressionDifficulty.SIMPLE,
        baseline_pass_rate=0.9,
        baseline_avg_attempts=1.2,
        source_experiment_id="OMN-11241-ctx-matrix",
        source_ticket_id="OMN-11241",
        baseline_source_path=".onex_state/hackathon/context_matrix/baseline.json",
        baseline_captured_at="2026-05-20T00:00:00Z",
    ),
    ModelRegressionTask(
        task_id="reg-004",
        description="Generate a compute node that validates a contract YAML against required fields",
        expected_difficulty=EnumRegressionDifficulty.NORMAL,
        baseline_pass_rate=0.8,
        baseline_avg_attempts=1.5,
        source_experiment_id="OMN-11241-ctx-matrix",
        source_ticket_id="OMN-11241",
        baseline_source_path=".onex_state/hackathon/context_matrix/baseline.json",
        baseline_captured_at="2026-05-20T00:00:00Z",
    ),
    ModelRegressionTask(
        task_id="reg-005",
        description="Generate a compute node that produces a JSON schema from a Pydantic model definition",
        expected_difficulty=EnumRegressionDifficulty.NORMAL,
        baseline_pass_rate=0.75,
        baseline_avg_attempts=1.8,
        source_experiment_id="OMN-11241-ctx-matrix",
        source_ticket_id="OMN-11241",
        baseline_source_path=".onex_state/hackathon/context_matrix/baseline.json",
        baseline_captured_at="2026-05-20T00:00:00Z",
    ),
    ModelRegressionTask(
        task_id="reg-006",
        description="Generate a compute node that emits a structured event envelope on completion",
        expected_difficulty=EnumRegressionDifficulty.NORMAL,
        baseline_pass_rate=0.7,
        baseline_avg_attempts=2.1,
        source_experiment_id="OMN-11241-ctx-matrix",
        source_ticket_id="OMN-11241",
        baseline_source_path=".onex_state/hackathon/context_matrix/baseline.json",
        baseline_captured_at="2026-05-20T00:00:00Z",
    ),
    ModelRegressionTask(
        task_id="reg-007",
        description=(
            "Generate a new ONEX handler with contract.yaml that subscribes to "
            "an input topic, processes a payload, and publishes to an output topic"
        ),
        expected_difficulty=EnumRegressionDifficulty.TOPOLOGY_AFFECTING,
        baseline_pass_rate=0.5,
        baseline_avg_attempts=2.8,
        source_experiment_id="OMN-11241-ctx-matrix",
        source_ticket_id="OMN-11241",
        baseline_source_path=".onex_state/hackathon/context_matrix/baseline.json",
        baseline_captured_at="2026-05-20T00:00:00Z",
    ),
)


__all__ = [
    "REGRESSION_TASKS",
    "EnumRegressionDifficulty",
    "ModelRegressionTask",
]
