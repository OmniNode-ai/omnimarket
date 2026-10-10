# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rows and report for the OCC retirement S7 replay (OMN-20917).

A replay reads, for each of a repository's last merged PRs (newest first),
OCC's recorded check run on the PR's head and the new path's verdict for the
PR's own contract at that head, and classifies the pair with the S5 rules
(``services/occ_verdict_difference.py``). ``ModelOccReplayRecord`` is the
recorded input of one PR, so a replay can be re-classified from a file without
reading anything; ``ModelOccReplayReport`` is the classified output.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_occ_verdict_difference_reason import (
    EnumOccVerdictDifferenceReason,
)
from omnimarket.nodes.node_dod_verify.models.model_occ_verdict_difference import (
    ModelNewPathVerdict,
    OccDifferenceOutcome,
)

#: The check run the receipt gate's difference step reads by default.
DEFAULT_OCC_CONTEXT = "occ-preflight / eligibility"


class ModelOccReplayRequest(BaseModel):
    """Replay one repository's last ``count`` compared merged PRs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(..., pattern=r"^[\w.-]+/[\w.-]+$")
    count: int = Field(default=30, ge=1)
    occ_context: str = DEFAULT_OCC_CONTEXT
    # How many merged PRs, newest first, the replay may examine while it
    # widens the window to reach ``count`` compared rows.
    max_examined: int = Field(default=200, ge=1)
    work_dir: Path
    # Interpreter that runs the verifier entrypoint at each head and control.
    verifier_python: Path
    # Seconds one verifier run may take before it counts as a failed run.
    verifier_timeout_s: int = Field(default=1800, ge=1)


class ModelOccReplayRecord(BaseModel):
    """One merged PR's recorded OCC check run and new-path verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: int
    head_sha: str
    merged_at: str = ""
    tickets: tuple[str, ...] = ()
    # The OCC check run (conclusion and annotation messages), None when the
    # head carries no such run (pre-OCC, or a skipped workflow).
    occ_check_run: dict[str, object] | None = None
    # None when the new path could not be replayed at this head.
    new_verdict: ModelNewPathVerdict | None = None
    negative_control: bool = False
    # Why the new path reached its verdict, and where the OCC run was read.
    note: str = ""


class ModelOccReplayRow(BaseModel):
    """One classified PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: int
    head_sha: str
    merged_at: str
    tickets: tuple[str, ...]
    occ_admitted: bool | None
    occ_conclusion: str | None
    occ_reason: str | None
    new_admitted: bool | None
    new_reason: str | None
    outcome: OccDifferenceOutcome
    reason_code: EnumOccVerdictDifferenceReason | None
    passed: bool
    note: str


class ModelOccReplaySummary(BaseModel):
    """Counts over the compared rows, and how far back the window went."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    target: int
    examined: int
    compared: int
    not_compared: int
    target_met: bool
    # Rows that fail the replay: unclassified_difference, and the forbidden
    # accepted_negative_control and old_behavioral_refusal.
    unclassified: int
    forbidden: int
    passed: bool
    by_outcome: dict[str, int]
    # A compared row's reason code; ``agree`` for an agreeing row.
    by_reason_code: dict[str, int]
    window_newest_pr: int | None
    window_oldest_pr: int | None
    window_oldest_merged_at: str


class ModelOccReplayReport(BaseModel):
    """Every examined row, newest first, and the summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelOccReplayRow, ...]
    summary: ModelOccReplaySummary


__all__ = [
    "DEFAULT_OCC_CONTEXT",
    "ModelOccReplayRecord",
    "ModelOccReplayReport",
    "ModelOccReplayRequest",
    "ModelOccReplayRow",
    "ModelOccReplaySummary",
]
