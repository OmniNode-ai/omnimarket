# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Frozen projection and pack-build evidence for live compaction assignment.

Install receipts supply package identity and installed_messages_hash only. Stamps
come exclusively from the corresponding pack build manifest, supplied by the
projection reader. This compute node performs no file or network I/O.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Nonempty = Annotated[str, Field(min_length=1)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Arm = Literal["on", "off"]
IncidentKey = tuple[str, str, str, str]
StampEvidence = Literal["ABSENT", "PRESENT", "CONTRADICTED", "UNAVAILABLE"]


class ModelFrozenRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelHookProjectionEvent(ModelFrozenRow):
    event_id: Nonempty
    event_time: AwareDatetime
    session_id: Nonempty
    role: Nonempty


class ModelInstalledBoundary(ModelFrozenRow):
    session_id: Nonempty
    boundary_uuid: Nonempty
    event_id: Nonempty
    event_time: AwareDatetime
    assistant_turn: int = Field(ge=0)
    package_id: Nonempty
    installed_messages_hash: Hash


class ModelPackBuildManifest(ModelFrozenRow):
    """A reader's pack-build manifest, joined to an install by both identity fields.

    An empty rendered_stamps is affirmative evidence that this readable manifest
    has no stamps. An unreadable manifest must be omitted, never replaced by {}.
    source_ref names the pack-build artifact, not the install receipt projection.
    """

    package_id: Nonempty
    installed_messages_hash: Hash
    source_ref: Nonempty
    rendered_stamps: dict[str, str]
    #: Explicit evidence of a contradicted rendering; different wording alone
    #: cannot establish contradiction and must never be counted as an incident.
    contradicted_item_keys: tuple[Nonempty, ...] = ()


class ModelIncidentSignal(ModelFrozenRow):
    event_id: Nonempty
    event_time: AwareDatetime
    session_id: Nonempty
    boundary_uuid: Nonempty
    item_key: Nonempty
    assistant_turn: int = Field(ge=0)
    kind: Literal[
        "act_against_ruling",
        "act_against_hold",
        "act_against_scope",
        "contradicted_decision",
        "question_guard_refusal",
        "operator_restatement",
    ]
    in_force: bool
    expected_stamp: Nonempty
    source: Literal["ledger", "hook_event"]


class ModelArmAssignmentRequest(ModelFrozenRow):
    correlation_id: UUID | None = None
    experiment_id: Nonempty
    seed: Nonempty
    window_turns: int = Field(gt=0)
    ledger_lane: Nonempty
    ledger_ticket: Nonempty
    friction_cost_estimate: Nonempty
    hook_events: tuple[ModelHookProjectionEvent, ...]
    boundaries: tuple[ModelInstalledBoundary, ...] = ()
    build_manifests: tuple[ModelPackBuildManifest, ...] = ()
    signals: tuple[ModelIncidentSignal, ...] = ()


class ModelSessionArm(ModelFrozenRow):
    session_id: str
    first_event_id: str
    block_index: int
    assigned_arm: Arm
    arm: Arm


class ModelExperimentMarker(ModelFrozenRow):
    """Idempotent update of the existing first hook-event projection row."""

    event_id: str
    session_id: str
    experiment_id: str
    experiment_start: Literal[True] = True
    arm: Arm


class ModelBoundaryPackageEvidence(ModelFrozenRow):
    session_id: str
    boundary_uuid: str
    installed_messages_hash: str
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    manifest_source_ref: str | None = None


class ModelAttributedIncident(ModelFrozenRow):
    dedup_key: IncidentKey
    event_id: str
    event_time: AwareDatetime
    stamp_evidence: Literal["ABSENT", "CONTRADICTED"]
    manifest_source_ref: str


class ModelArmAssignmentResult(ModelFrozenRow):
    correlation_id: UUID | None = None
    experiment_id: str
    assignments: tuple[ModelSessionArm, ...]
    markers: tuple[ModelExperimentMarker, ...]
    incidents: tuple[ModelAttributedIncident, ...]
    boundary_evidence: tuple[ModelBoundaryPackageEvidence, ...]
    kill_switch: bool
    disabled_at_event_id: str | None
    ledger_request_id: UUID | None
    ledger_rows: tuple[str, ...]
    on_boundaries: int
    unreadable_on_boundaries: int
    status: Literal["READY", "HOLD"]
    hold_reasons: tuple[Literal["NO_PACKAGE_EVIDENCE"], ...]
