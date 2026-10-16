# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed compaction replay manifest inputs and outputs (OMN-19998).

Inputs are the hook-event rows and the session-content rows the caller reads, plus ledger files
as bytes. No transcript file is read.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumBoundaryHook(StrEnum):
    PRE = "PreCompact"
    POST = "PostCompact"


class EnumSummaryStatus(StrEnum):
    """A hashed summary is not text: an id that resolves to no content row is `UNAVAILABLE`."""

    RESOLVED = "RESOLVED"
    UNAVAILABLE = "SUMMARY_UNAVAILABLE"
    NOT_CAPTURED = "NOT_CAPTURED"


class EnumRepeatClass(StrEnum):
    """Why an operator repeated a statement: a boundary came between, or none did."""

    AFTER_BOUNDARY = "AFTER_BOUNDARY"
    NO_BOUNDARY_BETWEEN = "NO_BOUNDARY_BETWEEN"


class ModelHookBoundaryEvent(_Frozen):
    """One PreCompact or PostCompact hook event as the projection holds it."""

    event_id: str
    session_id: str
    hook_event_name: EnumBoundaryHook
    emitted_at: AwareDatetime
    trigger: str
    lane: str | None = None
    model: str | None = None
    summary_length: int | None = None
    summary_sha256: str | None = None
    summary_record_id: str | None = None


class ModelCapturedRecord(_Frozen):
    """One content-capture row (prompt, operator_prompt or assistant_reply), redacted text."""

    event_id: str
    session_id: str
    content_kind: str
    chunk_index: int = 0
    chunk_count: int = 1
    emitted_at: AwareDatetime
    content: str
    content_sha256: str | None = None


class ModelLedgerFile(_Frozen):
    """The ledger file or one archive roll, as bytes."""

    name: str
    content: bytes


class ModelBoundaryPair(_Frozen):
    session_id: str
    pre_event_id: str
    pre_at: AwareDatetime
    post_event_id: str | None
    post_at: AwareDatetime | None
    trigger: str
    paired: bool
    window_start: AwareDatetime | None = Field(
        description="The previous paired PostCompact of the session, or None for session start."
    )
    summary_status: EnumSummaryStatus
    summary_length: int | None = None
    summary_sha256: str | None = None
    summary_record_id: str | None = None


class ModelBoundaryPairingRequest(_Frozen):
    events: tuple[ModelHookBoundaryEvent, ...]
    resolvable_record_ids: tuple[str, ...] = ()


class ModelBoundaryPairing(_Frozen):
    pairs: tuple[ModelBoundaryPair, ...]
    unpaired_pre: tuple[str, ...] = Field(
        description="PreCompact event ids with no PostCompact."
    )
    unpaired_post: tuple[str, ...] = Field(
        description="PostCompact event ids with no PreCompact."
    )


class ModelPromptClassification(_Frozen):
    event_id: str
    emitted_at: AwareDatetime
    operator: bool
    method: Literal["operator_prompt", "heuristic"]


class ModelPromptWindowRequest(_Frozen):
    """Classify the operator prompts of a session window."""

    records: tuple[ModelCapturedRecord, ...]
    window_start: AwareDatetime | None
    window_end: AwareDatetime
    operator_prompt_capture_start: AwareDatetime = Field(
        description="Earliest `operator_prompt` row anywhere in the projection (a caller read it)."
    )


class ModelPromptWindow(_Frozen):
    prompts: tuple[ModelPromptClassification, ...]
    operator_count: int
    exact: bool = Field(
        description="True when the `operator_prompt` kind covers the window."
    )
    heuristic_precision: float | None = None
    heuristic_recall: float | None = None


class ModelRepeatRequest(_Frozen):
    session_id: str
    first_at: AwareDatetime
    repeat_at: AwareDatetime
    events: tuple[ModelHookBoundaryEvent, ...]


class ModelRepeatVerdict(_Frozen):
    verdict: EnumRepeatClass
    boundaries_between: tuple[str, ...] = ()


class ModelExpectedStatement(_Frozen):
    """An operator statement a package must carry, by the exact words captured."""

    statement_id: str
    prompt_event_id: str
    ledger_stamp: str | None = Field(
        default=None,
        description="Stamp of the RULING row recording it, None when unrecorded.",
    )
    quote: str | None = Field(
        default=None, description="The exact words, when quotable."
    )
    required_spans: tuple[str, ...] = Field(
        default=(),
        description="Exact substrings of the prompt, for a prompt quoted in part.",
    )
    withheld_reason: str | None = None


class ModelPostBoundaryEvidence(_Frozen):
    event_id: str
    kind: Literal["RESTATED", "REASKED", "ACTED_AGAINST"]
    required_spans: tuple[str, ...]


class ModelCaseSpec(_Frozen):
    """The hand-written half of a case: scoring, written before any run."""

    case_id: str
    finding: str
    census_status: Literal["CONFIRMED", "PLAUSIBLE"]
    census_ref: str
    session_id: str
    boundary_pre_event_ids: tuple[str, ...] = Field(min_length=1)
    expected_statements: tuple[ModelExpectedStatement, ...] = Field(min_length=1)
    post_boundary_evidence: tuple[ModelPostBoundaryEvidence, ...] = Field(min_length=1)
    question: str
    expected_answer: str


class ModelBuildManifestRequest(_Frozen):
    specs: tuple[ModelCaseSpec, ...] = Field(min_length=1)
    events: tuple[ModelHookBoundaryEvent, ...]
    records: tuple[ModelCapturedRecord, ...]
    ledger_files: tuple[ModelLedgerFile, ...]
    resolvable_record_ids: tuple[str, ...] = ()
    census_ref: str
    profile_version: str | None = None


class ModelBundleFile(_Frozen):
    path: str
    content: bytes


class ModelBuildManifestResult(_Frozen):
    manifest_yaml: str
    bundle_files: tuple[ModelBundleFile, ...]
    case_ids: tuple[str, ...]
    refusals: tuple[str, ...] = ()


class ModelVerifyBundleRequest(_Frozen):
    manifest_yaml: str
    bundle_files: tuple[ModelBundleFile, ...]


class ModelVerifyBundleResult(_Frozen):
    ok: bool
    cases_checked: int
    failures: tuple[str, ...] = ()
