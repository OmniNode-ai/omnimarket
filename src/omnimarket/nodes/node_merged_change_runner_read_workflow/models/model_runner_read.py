# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and outputs of the merged-change runner read workflow.

The workflow reads every line the ``merged_change_runners`` overlay declares, one activity per line through
``node_merged_change_reading_effect``, and hands the readings to the publish step, which turns them into one
``ModelIntegrationReadingBatch`` for ``node_integration_reading_publish_effect``. A line the reader could not read is
carried as unread with its cause, never as running.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.integration_reading import (
    EnumIntegrationGapClass,
    EnumIntegrationState,
    ModelIntegrationReadingBatch,
)
from omnimarket.models.model_merged_change_reading import ModelMcrReading


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelRunnerDeclaredLine(BaseModel):
    """One runner row of the overlay; only the key is read here, the reader reads the rest."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    repo: str = Field(min_length=1)
    number: int = Field(gt=0)
    kind: str = Field(min_length=1)
    unit: str = ""
    host: str = ""

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


class ModelRunnerDeclaredLines(BaseModel):
    """The runner overlay as this node reads it: its declared lines."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    runners: tuple[ModelRunnerDeclaredLine, ...] = ()


class ModelRunnerReadSteps(_Frozen):
    """The node commands the workflow's one activity runs, as argv."""

    lines: tuple[str, ...] = Field(min_length=1)
    read_line: tuple[str, ...] = Field(min_length=1)
    publish: tuple[str, ...] = Field(min_length=1)


class ModelRunnerReadPlan(_Frozen):
    """One full read: the steps, the concurrency, the timeouts and the retry limit."""

    steps: ModelRunnerReadSteps
    line_concurrency: int = Field(ge=1)
    step_timeout_seconds: int = Field(ge=1)
    step_max_attempts: int = Field(ge=1)
    publish: bool = True


class ModelRunnerLineOutcome(_Frozen):
    """One declared line after its activity: the reader's reading, or the cause it is unread."""

    repo: str = Field(min_length=1)
    number: int = Field(gt=0)
    reading: ModelMcrReading | None = None
    unread_cause: str = ""
    read_at: datetime
    attempts: int = Field(ge=0, default=0)

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


class ModelRunnerReadPublishInput(_Frozen):
    """What the publish step gets: every line's outcome, the run's start, its id and its read time."""

    as_of: datetime
    run_id: str = Field(min_length=1)
    read_seconds: float = Field(ge=0)
    outcomes: tuple[ModelRunnerLineOutcome, ...]
    publish: bool = True


class ModelMergeFacts(_Frozen):
    """A change's merge as its canonical clone records it; empty when the clone could not be read."""

    title: str = ""
    merged_at: datetime | None = None


class ModelRunnerStateRule(_Frozen):
    """What a missing step means for the integration projection: its state and its gap class."""

    state: EnumIntegrationState
    gap_class: EnumIntegrationGapClass


class ModelRunnerIntegrationConfig(_Frozen):
    """The contract's mapping from a reader verdict to an integration reading."""

    verdict_running: str = Field(min_length=1)
    verdict_not_running: str = Field(min_length=1)
    verdict_unread: str = Field(min_length=1)
    library_kind: str = Field(min_length=1)
    target_runtime: str = Field(min_length=1)
    target_unit_template: str = Field(min_length=1)
    store_running_path: str = Field(min_length=1)
    missing_steps: dict[str, ModelRunnerStateRule]
    unknown_missing_step: ModelRunnerStateRule


class ModelRunnerIntegrationRequest(_Frozen):
    """The pure mapping's input: the outcomes, each change's merge facts and the declared host of each unit line."""

    as_of: datetime
    origin: str = Field(min_length=1)
    outcomes: tuple[ModelRunnerLineOutcome, ...]
    merge_facts: dict[str, ModelMergeFacts] = Field(default_factory=dict)
    unit_targets: dict[str, str] = Field(default_factory=dict)
    config: ModelRunnerIntegrationConfig


class ModelRunnerReadCounts(_Frozen):
    """The verdict counts of one full read."""

    declared: int = Field(ge=0)
    seen_running: int = Field(ge=0)
    not_running: int = Field(ge=0)
    unread: int = Field(ge=0)


class ModelRunnerIntegrationResult(_Frozen):
    """The batch for the publish effect and the read's counts."""

    batch: ModelIntegrationReadingBatch
    counts: ModelRunnerReadCounts


__all__ = [
    "ModelMergeFacts",
    "ModelRunnerDeclaredLine",
    "ModelRunnerDeclaredLines",
    "ModelRunnerIntegrationConfig",
    "ModelRunnerIntegrationRequest",
    "ModelRunnerIntegrationResult",
    "ModelRunnerLineOutcome",
    "ModelRunnerReadCounts",
    "ModelRunnerReadPlan",
    "ModelRunnerReadPublishInput",
    "ModelRunnerReadSteps",
    "ModelRunnerStateRule",
]
