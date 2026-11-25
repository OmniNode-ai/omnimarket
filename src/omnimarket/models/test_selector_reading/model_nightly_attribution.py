# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types of the nightly attribution: a red test file on the dev head, the merged pull requests
whose executed selection skipped it, and the completeness of the night that produced the result.

The attribution is a detector with named limits, not proof of cause: it cannot see a failure that a
later merge fixed before the nightly ran, it cannot see tests marked ``kafka`` (the nightly runs
``-m "not kafka"``), and a cause shared by several suspects is named as several suspects.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.test_selector_reading.model_test_selector_reading import (
    EnumSelectorReadingState,
)

#: Written into every verdict and every alarm text (the plan names both limits).
ATTRIBUTION_LIMITS: tuple[str, ...] = (
    "detector, not proof of cause: several suspects may share one cause",
    "a failure fixed by a later merge before the nightly ran is not seen",
    "tests marked kafka are not in the nightly and are outside detection",
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumFileOutcome(StrEnum):
    """One test file's outcome in one night's junit."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"
    ABSENT = "absent"


class EnumAttributionClass(StrEnum):
    MISS = "miss"
    INTERACTION = "interaction"
    PREEXISTING = "preexisting"
    UNATTRIBUTABLE = "unattributable"
    #: The night's results are not complete; no file is attributed from them.
    INCOMPLETE = "incomplete"


class EnumUnattributableReason(StrEnum):
    #: The file was absent the night before (a new test, or one deselected).
    NO_PRIOR_RESULT = "no_prior_result"
    #: The file passed the night before and no commit landed in between.
    NO_CANDIDATE = "no_candidate"
    #: A candidate has no usable evidence and none skipped the file.
    NO_EVIDENCE = "no_evidence"
    #: As NO_EVIDENCE, and a candidate in the range is a push with no pull request record.
    DIRECT_PUSH = "direct_push"


class EnumEvidenceState(StrEnum):
    """What a candidate's evidence is worth. Anything but USABLE is no evidence, never a pass."""

    USABLE = "usable"
    DIRECT_PUSH = "direct_push"
    MISSING_RECORD = "missing_record"
    MISSING_RUN = "missing_run"
    RECORD_MISMATCH = "record_mismatch"
    CANCELLED_RUN = "cancelled_run"
    FINISHED_AFTER_MERGE = "finished_after_merge"
    SKIPPED_SHARD = "skipped_shard"
    SHARD_NOT_SUCCESS = "shard_not_success"
    MISSING_STAGE_ENTRY = "missing_stage_entry"
    #: A shadow read: the executed selection skipped the file, so its run is no pass for it.
    EXECUTED_SKIPPED_FILE = "executed_skipped_file"


class EnumIncompleteReason(StrEnum):
    RUN_NOT_TERMINAL = "run_not_terminal"
    MISSING_ARTIFACTS = "missing_artifacts"
    SHARD_IN_ERROR = "shard_in_error"
    TOO_FEW_TEST_FILES = "too_few_test_files"


class EnumShardConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ModelNightCompleteness(_Frozen):
    """What the collector read of the night's run, before any file is attributed."""

    run_terminal: bool
    shards_expected: int = Field(ge=1)
    #: Distinct ``nightly-test-results-<n>`` artifacts found.
    artifacts_found: int = Field(ge=0)
    #: Shards with a collection or runner error (even when no assertion failed).
    shards_in_error: int = Field(ge=0)
    test_files_reported: int = Field(ge=0)
    min_test_files: int = Field(ge=0)


class ModelCandidateSelection(_Frozen):
    """The paths one selection ran: a directory (trailing slash) or one test file."""

    selected_paths: tuple[str, ...] = Field(min_length=1)
    split_count: int = Field(ge=1)


class ModelCandidateRecord(_Frozen):
    """The part of a pull request's selection record the attribution reads."""

    checkout_sha: str = Field(min_length=1)
    executed: ModelCandidateSelection
    #: Stage -> what the rules at or below that stage would run (read by a shadow run).
    by_stage: dict[int, ModelCandidateSelection] = Field(default_factory=dict)


class ModelCandidateRun(_Frozen):
    """The authoritative run of a pull request: the latest attempt completed before the merge."""

    run_id: int = Field(ge=1)
    attempt: int = Field(ge=1)
    checkout_sha: str = Field(min_length=1)
    completed_at: AwareDatetime
    cancelled: bool = False
    #: Conclusion of each pytest shard job the run lists, keyed by shard number.
    shard_conclusions: dict[int, EnumShardConclusion] = Field(default_factory=dict)


class ModelAttributionCandidate(_Frozen):
    """One first-parent commit between the two nights' tested heads."""

    commit_sha: str = Field(min_length=1)
    #: None for a direct push: no pull request record exists for it.
    pr_number: int | None = Field(default=None, ge=1)
    merged_at: AwareDatetime
    record: ModelCandidateRecord | None = None
    run: ModelCandidateRun | None = None


class ModelNightlyAttributionRequest(_Frozen):
    repo: str = Field(min_length=1)
    slot_date: date
    completeness: ModelNightCompleteness
    #: Test file -> outcome in tonight's junit (files absent from the dict are absent).
    outcomes: dict[str, EnumFileOutcome]
    #: Test file -> outcome in the previous night's junit.
    previous_outcomes: dict[str, EnumFileOutcome]
    candidates: tuple[ModelAttributionCandidate, ...] = ()
    #: None reads each candidate's executed selection. A stage reads that stage's entry and labels the
    #: result shadow ("would skip"), so evidence for the next stage exists before narrowing is active.
    shadow_stage: int | None = Field(default=None, ge=0, le=4)

    @model_validator(mode="after")
    def _candidates_are_distinct(self) -> ModelNightlyAttributionRequest:
        shas = [c.commit_sha for c in self.candidates]
        if len(set(shas)) != len(shas):
            raise ValueError("a commit appears twice among the candidates")
        return self


class ModelCandidateEvidence(_Frozen):
    commit_sha: str
    pr_number: int | None
    state: EnumEvidenceState


class ModelFileAttribution(_Frozen):
    test_file: str
    klass: EnumAttributionClass
    reason: EnumUnattributableReason | None = None
    #: Every candidate whose selection skipped the file (a miss only), sorted by commit.
    suspects: tuple[str, ...] = ()
    #: Candidates with no usable evidence, listed beside the suspects, sorted by commit.
    no_evidence: tuple[ModelCandidateEvidence, ...] = ()
    shadow: bool = False


class ModelNightlyAttribution(_Frozen):
    repo: str
    slot_date: date
    complete: bool
    incomplete_reasons: tuple[EnumIncompleteReason, ...] = ()
    attributions: tuple[ModelFileAttribution, ...] = ()
    #: NOT WORKING when any file is a miss, UNKNOWN when the night is incomplete, else WORKING.
    reading_state: EnumSelectorReadingState
    limits: tuple[str, ...] = ATTRIBUTION_LIMITS
