# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the compaction ground-truth compute node.

The request carries the pinned ledger prefix as raw rows and everything a session
adds that the ledger does not hold (its orchestrator lane, its window, spent consents);
the result is the must-survive set at the boundary instant, each item with the stamps
of the rows that cover it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_STAMP = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumGroundTruthKind(StrEnum):
    RULING = "RULING"
    OPERATOR_CONSENT = "OPERATOR_CONSENT"
    OPEN_CLAIM = "OPEN_CLAIM"
    OPEN_ASK = "OPEN_ASK"
    PROMISE_SENT = "PROMISE_SENT"
    DRAFT_UNSENT = "DRAFT_UNSENT"
    HOLD = "HOLD"
    UNACKED_MSG = "UNACKED_MSG"


class EnumExpectedConduct(StrEnum):
    """What later work must do for an item that survived the compaction."""

    NOT_ACT_AGAINST = "not_act_against"
    STAY_IN_SCOPE = "stay_in_scope"
    TRACK_TO_TERMINAL = "track_to_terminal"
    CARRY_PENDING = "carry_pending"
    FOLLOW_UP_EXISTS = "follow_up_exists"
    SEND_OR_SURFACE = "send_or_surface"
    NOT_ACT_ON_SCOPE = "not_act_on_scope"
    ACKNOWLEDGE_OR_ACT = "acknowledge_or_act"


EXPECTED_CONDUCT: dict[EnumGroundTruthKind, EnumExpectedConduct] = {
    EnumGroundTruthKind.RULING: EnumExpectedConduct.NOT_ACT_AGAINST,
    EnumGroundTruthKind.OPERATOR_CONSENT: EnumExpectedConduct.STAY_IN_SCOPE,
    EnumGroundTruthKind.OPEN_CLAIM: EnumExpectedConduct.TRACK_TO_TERMINAL,
    EnumGroundTruthKind.OPEN_ASK: EnumExpectedConduct.CARRY_PENDING,
    EnumGroundTruthKind.PROMISE_SENT: EnumExpectedConduct.FOLLOW_UP_EXISTS,
    EnumGroundTruthKind.DRAFT_UNSENT: EnumExpectedConduct.SEND_OR_SURFACE,
    EnumGroundTruthKind.HOLD: EnumExpectedConduct.NOT_ACT_ON_SCOPE,
    EnumGroundTruthKind.UNACKED_MSG: EnumExpectedConduct.ACKNOWLEDGE_OR_ACT,
}


class ModelGroundTruthRequest(_Frozen):
    """One boundary: the pinned ledger prefix, the instant ``now`` and the session scope."""

    session_id: str = Field(min_length=1)
    ledger_prefix: tuple[str, ...]
    now: str = Field(pattern=_STAMP)
    window_start: str = Field(pattern=_STAMP)
    # The session's orchestrator lane (CM.1 records the attribution rule and evidence).
    orchestrator_lanes: tuple[str, ...] = Field(min_length=1)
    # Lanes of the family the CLAIM and MSG ``parent=`` cells do not name.
    extra_lanes: tuple[str, ...] = ()
    operator_names: tuple[str, ...] = ("operator",)
    # The ledger records no spend of an OPERATOR-CONSENT; the caller names the spent ones.
    spent_consent_stamps: tuple[str, ...] = ()


class ModelGroundTruthRow(_Frozen):
    """One ledger row that covers an item, by its stamp and content hash."""

    stamp: str = Field(pattern=_STAMP)
    row_type: str
    lane: str | None = None
    row_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Literal["primary", "amender", "extender", "correction", "evidence"]
    quote: str = ""
    operator_span: str | None = None


class ModelGroundTruthItem(_Frozen):
    kind: EnumGroundTruthKind
    key: str
    quote: str
    required_spans: tuple[str, ...] = Field(min_length=1)
    expected_conduct: EnumExpectedConduct
    rows: tuple[ModelGroundTruthRow, ...] = Field(min_length=1)


class ModelKindCount(_Frozen):
    kind: EnumGroundTruthKind
    count: int


class ModelGroundTruthResult(_Frozen):
    session_id: str
    now: str
    window_start: str
    lane_family: tuple[str, ...]
    prefix_row_count: int
    rows_as_of_count: int
    refused_row_count: int
    prefix_hash: str
    items: tuple[ModelGroundTruthItem, ...]
    counts_by_kind: tuple[ModelKindCount, ...]
    set_hash: str


class ModelKindExpectation(_Frozen):
    """The plan author's audit of one kind: row stamps that must and must not be present."""

    kind: EnumGroundTruthKind
    positives: tuple[str, ...] = ()
    negatives: tuple[str, ...] = ()


class ModelLabelledProposition(_Frozen):
    """A TE.1 labelled proposition at one boundary, with its ``ledger_coverage`` cell.

    ``covered_by`` is the cell as ``labels.yaml`` writes it (``RULING 2026-10-15T03:37:58Z``),
    or None when no ledger row covers the proposition.
    """

    case_id: str
    boundary: int
    proposition_id: str
    covered_by: str | None = None


class ModelAuditFinding(_Frozen):
    kind: EnumGroundTruthKind | None = None
    stamp: str | None = None
    proposition_id: str | None = None
    detail: str


class ModelGroundTruthAuditRequest(_Frozen):
    result: ModelGroundTruthResult
    expectations: tuple[ModelKindExpectation, ...] = ()
    propositions: tuple[ModelLabelledProposition, ...] = ()


class ModelGroundTruthAuditResult(_Frozen):
    set_hash: str
    items_total: int
    prefix_row_count: int
    rows_as_of_count: int
    missing_positives: tuple[ModelAuditFinding, ...]
    present_negatives: tuple[ModelAuditFinding, ...]
    unexpected_items: tuple[ModelAuditFinding, ...]
    uncovered_propositions: tuple[ModelAuditFinding, ...]
    size_equals_prefix: bool
    passed: bool
