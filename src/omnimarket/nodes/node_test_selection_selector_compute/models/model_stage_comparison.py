# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stage comparison: the narrowed selection of every recorded change against the failures its full run recorded.

One comparison is a pure function of a replay result (what the selector selects for each change) and the recorded
full-suite runs (which tests failed for each change). It replaces the multi-night wait of the stage gate: a stage
may be entered when the comparison over the recorded set passes, that is, the selection holds every test that
failed in the full run of the same change.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelReplayResult,
)

_HEX40 = r"^[0-9a-f]{40}$"


class ModelRecordedFullRun(BaseModel):
    """The recorded full-suite result of one change: its failing tests, or that no result was readable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The merge the run belongs to, the key into the replay's decisions.
    sha: str = Field(pattern=_HEX40)
    #: False when the source holds no readable result for the change; such a change is counted, never compared.
    readable: bool
    #: The run's conclusion when readable (``success`` or a failing conclusion).
    conclusion: str | None = None
    #: Test node ids (``path::name`` or ``path``) the run recorded as failed.
    failing_tests: tuple[str, ...] = ()
    #: Minutes between the run's start and its last update; None when the source does not hold both times.
    wall_minutes: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _unreadable_holds_no_result(self) -> ModelRecordedFullRun:
        if not self.readable and (self.conclusion or self.failing_tests):
            raise ValueError(
                "a run that is not readable holds no conclusion and no failing test"
            )
        if self.readable and self.conclusion is None:
            raise ValueError("a readable run names its conclusion")
        return self


class ModelStageComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    replay: ModelReplayResult
    runs: tuple[ModelRecordedFullRun, ...]
    #: Fewest changes the comparison must cover; zero changes is never a pass.
    min_changes_compared: int = Field(ge=1)


class ModelComparisonMiss(BaseModel):
    """A test the full run of a change failed and the selection for that change would not have run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: str
    test: str
    selection_kind: str
    selected_paths: tuple[str, ...]


class ModelStageComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Changes the replay selected for.
    changes_compared: int = Field(ge=0)
    #: Of those, the changes whose selection is narrowed.
    changes_narrowed: int = Field(ge=0)
    #: Changes whose recorded full run was readable (the ones that can show a miss).
    runs_with_result: int = Field(ge=0)
    #: Changes with no readable full run.
    runs_unavailable: int = Field(ge=0)
    #: Recorded runs for a change the replay holds no decision for.
    runs_unmatched: int = Field(ge=0)
    #: Readable runs whose conclusion is not success.
    failing_runs: int = Field(ge=0)
    #: Failing runs that name no failing test; they cannot be compared and the comparison refuses them.
    failing_runs_unnamed: int = Field(ge=0)
    #: Failing tests across the readable runs.
    failing_tests: int = Field(ge=0)
    #: Of those, the ones the selection for their change would have run.
    failing_tests_selected: int = Field(ge=0)
    misses: tuple[ModelComparisonMiss, ...]
    #: Share of changes whose selection is narrowed, rounded to six places.
    merge_narrowed_share: float = Field(ge=0, le=1)
    #: Recorded test seconds of the selections, per change, rounded to six places.
    test_seconds_per_merge: float = Field(ge=0)
    #: Mean run wall minutes over the readable runs that hold both times; None when no run does.
    ci_minutes_per_pr: float | None = Field(default=None, ge=0)
    #: The comparison passes: no miss, no unnamed failure, no unmatched run, enough changes and at least one readable run.
    passed: bool
    #: Why it did not pass, apart from a miss (a miss is in ``misses``), in a fixed order.
    reasons: tuple[str, ...]

    @model_validator(mode="after")
    def _passed_means_no_reason_and_no_miss(self) -> ModelStageComparisonResult:
        if self.passed == bool(self.reasons or self.misses):
            raise ValueError(
                "passed is true exactly when there is no reason and no miss"
            )
        return self
