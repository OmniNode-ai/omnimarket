# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the landing proof gate.

Frozen Pydantic models reject undeclared fields. The policy carries every pattern and list the gate reads.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_landing_proof_gate_compute.models.enum_proof_gate import (
    EnumProofAnswer,
    EnumProofCode,
)

__all__ = [
    "EnumProofAnswer",
    "EnumProofCode",
    "ModelLedgerNotice",
    "ModelProofGatePolicy",
    "ModelProofGateRequest",
    "ModelProofGateResult",
    "ModelProofReason",
    "ModelProofText",
]


class ModelProofGatePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lab_line: str  # a regex; a line that matches is a Lab line
    sha: str  # a regex; a commit a Lab line names
    lab_failed: str  # a regex; a clause that matches records a refused or failed run, or a result that is not proven
    lab_clause_split: str  # a regex; a Lab line is read clause by clause, so one clause's pass never clears another's failure
    lab_known_bad: str  # a regex; a clause naming the known-bad control: its refusal or failure is the control working
    lab_known_good: str  # a regex; a clause naming the known-good control, which must itself carry no failure
    lab_baseline: str  # a regex; a clause about the base or the state before the change: the red of a red-first proof
    gated_words: tuple[
        str, ...
    ]  # a changed non-test path holding one touches a grader, check, gate or judge
    test_path_markers: tuple[str, ...]
    doc_path_markers: tuple[str, ...]
    doc_extensions: tuple[str, ...]
    duration_path_markers: tuple[str, ...]  # test-duration data files
    known_good: str  # a regex over one line: a known good answer that passed
    known_bad: str  # a regex over one line: a known bad answer that failed
    do_not_land: str  # a regex over one ledger row: a do-not-land instruction
    pr_ref: (
        str  # a regex template with {repo} and {number}: a row names the pull request
    )
    ledger_days: int  # how many days of ledger files are read


class ModelProofText(BaseModel):
    """What the proof reader read of one pull request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    body: str
    comments: tuple[str, ...] = ()
    files: tuple[str, ...] = ()


class ModelLedgerNotice(BaseModel):
    """An open HOLD row or do-not-land message naming the pull request, not lifted by a RELEASE."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    rtype: str
    ts: str


class ModelProofGateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    number: int
    base: str
    head: str
    # False when the caller has no ledger to give (a hosted workflow); the result then says the ledger was not checked
    check_ledger: bool = True


class ModelProofReason(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: EnumProofCode
    item: str  # the missing item, or the notice id
    detail: str = ""


class ModelProofGateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: EnumProofAnswer
    reasons: tuple[ModelProofReason, ...] = Field(default_factory=tuple)
    exempt: str = ""  # why no Lab line was asked for: docs, test_durations or both joined by a plus
    proof: str = (
        ""  # how the proof matched: head (a Lab commit is the head) or patch_id
    )
    ledger_checked: bool = True

    @property
    def admitted(self) -> bool:
        return self.answer is EnumProofAnswer.PROVEN

    def line(self) -> str:
        """One line for a log: the answer, then every reason's code and missing item."""
        if self.admitted:
            return (
                "PROVEN"
                + (f" exempt={self.exempt}" if self.exempt else "")
                + (f" proof={self.proof}" if self.proof else "")
            )
        return "DRAFT " + "; ".join(
            f"{r.code.value}: {r.item}" + (f" ({r.detail})" if r.detail else "")
            for r in self.reasons
        )
