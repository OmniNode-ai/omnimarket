# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start command for node_regression_test_orchestrator (OMN-13616).

The orchestrator's bus entrypoint. Carries the experiment identity plus the
recorded replay corpus the suite replays against. There is no live-generation
field: the canonical node runs in **replay mode only** — replay from a recorded
event corpus, deterministic, no I/O. Live generation (the SEA ``_run_live``
path) is intentionally NOT migrated; it belonged to the SEA imperative executor
and is replaced by the canonical generation pipeline elsewhere in epic
OMN-13604.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_regression_test_orchestrator.models.model_regression_replay_entry import (
    ModelRegressionReplayEntry,
)
from omnimarket.nodes.node_regression_test_orchestrator.models.model_regression_task import (
    REGRESSION_TASKS,
    ModelRegressionTask,
)


class ModelRegressionSuiteStart(BaseModel):
    """Start a deterministic regression replay via the orchestrator.

    ``correlation_id`` / ``run_id`` / ``experiment_id`` default when absent so the
    typed command validates against the runtime-injected envelope correlation_id
    on the canonical ``onex run-node`` dispatch path (mirrors
    ``ModelCloseoutStartCommand``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    experiment_id: UUID = Field(
        default_factory=uuid4, description="Unique identifier for this experiment."
    )
    run_id: UUID = Field(
        default_factory=uuid4, description="Identifier for this specific replay run."
    )
    correlation_id: UUID = Field(
        default_factory=uuid4, description="Correlation ID linking run events."
    )
    runtime_identity: str = Field(
        default="replay/runtime-local",
        min_length=1,
        description="Runtime lane/service identity executing the replay.",
    )
    samples_per_task: int = Field(
        default=1,
        ge=1,
        description=(
            "Samples represented per task. 1 marks results provisional "
            "(single stochastic run); >=2 is a multi-sample (non-provisional) corpus."
        ),
    )
    tasks: tuple[ModelRegressionTask, ...] = Field(
        default=REGRESSION_TASKS,
        description="The regression task suite to replay (defaults to REGRESSION_TASKS).",
    )
    replay_corpus: tuple[ModelRegressionReplayEntry, ...] = Field(
        default_factory=tuple,
        description=(
            "Recorded (task_id, attempt, output) entries. A task with no matching "
            "attempt-1 entry, or one whose output is empty, replays as failed."
        ),
    )


__all__ = ["ModelRegressionSuiteStart"]
