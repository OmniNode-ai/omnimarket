# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pre-registration block of the compaction thresholds data file.

Everything a confirmatory run is judged against is fixed here before the first
confirmatory run: the one-time split of sessions into a calibration set and a
held-out set (with its hash), the recorded calibration, the Jaccard tolerance of
the sandboxed continuation, the progress-call classes of each open-loop kind,
the build-selection rule, and per safeguard the smallest margin the held-out
session count can establish. No field has a default: a missing number is refused
by its name.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "CONFIRMATORY_SAFEGUARDS",
    "REQUIRED_OPEN_LOOP_KINDS",
    "EnumBuildSelectionRule",
    "EnumFloorDirection",
    "ModelBuildCandidate",
    "ModelBuildFloor",
    "ModelBuildSelection",
    "ModelCalibrationRecord",
    "ModelControlBoundary",
    "ModelDataPrRecord",
    "ModelPreregistration",
    "ModelSafeguardFeasibility",
    "ModelSessionSplit",
    "feasible_minimum_margin",
    "session_split_sha256",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")

#: The open-loop kinds of S2; each needs a list of call classes that count as progress.
REQUIRED_OPEN_LOOP_KINDS: tuple[str, ...] = (
    "OPEN_CLAIM",
    "OPEN_ASK",
    "PROMISE_SENT",
    "DRAFT_UNSENT",
    "UNACKED_MSG",
)

#: The noninferiority safeguards of section 3.8 that carry a margin in the data file
#: (the cost ratio is bounded by ``cost_ratio_limit``).
CONFIRMATORY_SAFEGUARDS: tuple[str, ...] = (
    "contradiction",
    "false_memory",
    "hold_scope_violation",
    "planted_recall",
)

_MARGIN_TOLERANCE = 1e-9


def feasible_minimum_margin(held_out_sessions: int) -> float:
    """The smallest margin the held-out session count can establish at zero events."""
    if held_out_sessions < 1:
        raise ValueError("no held-out session")
    return 3.0 / held_out_sessions


def session_split_sha256(
    calibration: Iterable[str], held_out: Iterable[str], seed: int
) -> str:
    """Hash of the split: the seed and both sorted id lists, in canonical JSON."""
    body = json.dumps(
        {
            "seed": seed,
            "calibration": sorted(calibration),
            "held_out": sorted(held_out),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class EnumBuildSelectionRule(StrEnum):
    """Among builds that meet every floor in its direction, the highest score of the
    selection metric on the calibration split; ties go to the later commit."""

    FLOORS_THEN_HIGHEST_CALIBRATION_SCORE_THEN_LATER_COMMIT = (
        "FLOORS_THEN_HIGHEST_CALIBRATION_SCORE_THEN_LATER_COMMIT"
    )


class EnumFloorDirection(StrEnum):
    HIGHER = "higher"
    LOWER = "lower"


class ModelSessionSplit(BaseModel):
    """The one-time, seeded split of sessions; disjoint, with its hash."""

    model_config = _FROZEN

    seed: int
    calibration_sessions: tuple[str, ...] = Field(min_length=1)
    held_out_sessions: tuple[str, ...] = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _disjoint_and_hashed(self) -> ModelSessionSplit:
        for name, ids in (
            ("calibration_sessions", self.calibration_sessions),
            ("held_out_sessions", self.held_out_sessions),
        ):
            if len(set(ids)) != len(ids):
                raise ValueError(f"{name} repeats a session id")
        shared = sorted(set(self.calibration_sessions) & set(self.held_out_sessions))
        if shared:
            raise ValueError(
                f"session {shared[0]!r} is in both the calibration and held-out sets"
            )
        expected = session_split_sha256(
            self.calibration_sessions, self.held_out_sessions, self.seed
        )
        if self.sha256 != expected:
            raise ValueError("sha256 does not match the recorded session sets and seed")
        return self


class ModelControlBoundary(BaseModel):
    model_config = _FROZEN

    session_id: str = Field(min_length=1)
    boundary_id: str = Field(min_length=1)


class ModelCalibrationRecord(BaseModel):
    """What the calibration run measured, from the calibration split only."""

    model_config = _FROZEN

    #: The boundaries the A/A, oracle, empty, inverted and no-context controls ran on.
    control_boundaries: tuple[ModelControlBoundary, ...] = Field(min_length=8)
    #: Half-width of the band the A/A control must lie inside.
    aa_band: float = Field(gt=0.0, lt=1.0)
    #: Noise floor: the spread of a score across repeats of one unchanged context.
    noise_floor: float = Field(ge=0.0, lt=1.0)
    judge_oracle_wilson_lower: float = Field(ge=0.0, le=1.0)
    judge_inverted_wilson_lower: float = Field(ge=0.0, le=1.0)
    simulated_power: float = Field(ge=0.0, le=1.0)
    corpus_sessions: int = Field(ge=1)


class ModelBuildFloor(BaseModel):
    model_config = _FROZEN

    criterion: str = Field(min_length=1)
    direction: EnumFloorDirection
    value: float


class ModelBuildSelection(BaseModel):
    """The one rule that picks the build a confirmatory run may judge."""

    model_config = _FROZEN

    rule: EnumBuildSelectionRule
    selection_metric: str = Field(min_length=1)
    floors: tuple[ModelBuildFloor, ...] = Field(min_length=1)


class ModelSafeguardFeasibility(BaseModel):
    """Per safeguard: the smallest margin the held-out count can establish and the margin."""

    model_config = _FROZEN

    name: str = Field(min_length=1)
    smallest_establishable_margin: float = Field(gt=0.0)
    margin: float = Field(gt=0.0, lt=1.0)

    @property
    def feasible(self) -> bool:
        return self.margin + _MARGIN_TOLERANCE >= self.smallest_establishable_margin


class ModelPreregistration(BaseModel):
    """Every number and list CM.14 fixes before the first confirmatory run."""

    model_config = _FROZEN

    split: ModelSessionSplit
    calibration: ModelCalibrationRecord
    jaccard_tolerance: float = Field(gt=0.0, le=1.0)
    progress_call_classes: dict[str, tuple[str, ...]]
    build_selection: ModelBuildSelection
    safeguard_feasibility: tuple[ModelSafeguardFeasibility, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _complete_and_consistent(self) -> ModelPreregistration:
        kinds = set(self.progress_call_classes)
        missing = [k for k in REQUIRED_OPEN_LOOP_KINDS if k not in kinds]
        if missing:
            raise ValueError(f"progress_call_classes has no list for {missing[0]}")
        extra = sorted(kinds - set(REQUIRED_OPEN_LOOP_KINDS))
        if extra:
            raise ValueError(f"progress_call_classes names unknown kind {extra[0]}")
        for kind, classes in self.progress_call_classes.items():
            if not classes or any(not c for c in classes):
                raise ValueError(f"progress_call_classes {kind} is an empty list")
        calibration = set(self.split.calibration_sessions)
        for control in self.calibration.control_boundaries:
            if control.session_id not in calibration:
                raise ValueError(
                    f"control boundary session {control.session_id!r} is not a "
                    "calibration session"
                )
        smallest = feasible_minimum_margin(len(self.split.held_out_sessions))
        for entry in self.safeguard_feasibility:
            if abs(entry.smallest_establishable_margin - smallest) > _MARGIN_TOLERANCE:
                raise ValueError(
                    f"safeguard {entry.name!r} smallest_establishable_margin "
                    f"{entry.smallest_establishable_margin} is not 3 / held-out "
                    f"count = {smallest}"
                )
        return self

    @property
    def infeasible_safeguards(self) -> tuple[str, ...]:
        return tuple(e.name for e in self.safeguard_feasibility if not e.feasible)


class ModelBuildCandidate(BaseModel):
    """One build of the mod and its scores on the calibration split."""

    model_config = _FROZEN

    candidate_id: str = Field(min_length=1)
    commit: str = Field(min_length=7)
    commit_time: datetime
    calibration_scores: dict[str, float]


class ModelDataPrRecord(BaseModel):
    """The merged data PR that fixed the data file, as the runner read it."""

    model_config = _FROZEN

    data_pr: str = Field(min_length=1)
    data_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    merged_at: datetime
