# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared routing facts and ports' wire types. Policy validation belongs to core."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRoutingValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelOwnerRule(ModelRoutingValue):
    repository: str = Field(min_length=1)
    paths: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()


class ModelOwnerMap(ModelRoutingValue):
    rules: tuple[ModelOwnerRule, ...] = Field(min_length=1)


class ModelReadEvidence(ModelRoutingValue):
    path: str
    text: str


class ModelLaneBlockFacts(ModelRoutingValue):
    blocker: str
    report_text: str
    owner_map: ModelOwnerMap
    files: tuple[ModelReadEvidence, ...] = ()


class ModelOwnerDiagnosis(ModelRoutingValue):
    owner: str | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_line: str = ""
    reason: str


class ModelBlockedLane(ModelRoutingValue):
    lane: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    outcome: Literal["blocked", "failed"]
    blocker: str
    report_path: str
    # The normalizer supplies the key; routing never reclassifies it.
    class_key: str = Field(min_length=1)
    head: str = Field(min_length=1)
    item_ids: tuple[str, ...] = Field(min_length=1)
    brief: str = ""
    subject_kind: Literal["item", "ticket", "pull_request"] = "item"


class ModelLaneRoutingItem(ModelRoutingValue):
    item_id: str = Field(min_length=1)
    text: str
    ticket: str | None = None
    repository: str | None = None
    head: str = "initial"
    subject_kind: Literal["item", "ticket", "pull_request"] = "item"


class ModelLaneRoutingRequest(ModelRoutingValue):
    operation: Literal["diagnose", "initial"]
    terminal: ModelBlockedLane | None = None
    item: ModelLaneRoutingItem | None = None
    # A completed read-only triage returns its report, never a bare guessed repo.
    triage_report: str | None = None


class ModelFillBacklogCommand(ModelRoutingValue):
    contract: Literal["fill-backlog-item"] = "fill-backlog-item"
    target_repo: str = Field(min_length=1)
    subject_kind: Literal["item", "ticket", "pull_request"]
    key: str
    class_key: str
    head: str
    for_items: tuple[str, ...]
    brief: str
    report_text: str


class ModelTriageCommand(ModelRoutingValue):
    key: str
    item_id: str
    text: str
    allow_code: Literal[False] = False
    worker: Literal[False] = False
    target_repo: None = None


class ModelRoutingClaim(ModelRoutingValue):
    key: str
    class_key: str
    owner: str
    head: str
    item_ids: tuple[str, ...]
    lane: str | None = None
    command: ModelFillBacklogCommand


class ModelRoutingState(ModelRoutingValue):
    claims: dict[str, ModelRoutingClaim] = Field(default_factory=dict)
    triages: dict[str, str | None] = Field(default_factory=dict)


class ModelLaneRoutingOverlay(ModelRoutingValue):
    owner_map: ModelOwnerMap
    report_roots: tuple[str, ...] = Field(min_length=1)
    repository_roots: tuple[str, ...] = Field(min_length=1)
    state_path: str
    max_open_triage: int = Field(ge=1)
    max_report_bytes: int = Field(ge=1)
    max_file_bytes: int = Field(ge=1)
    max_named_files: int = Field(ge=1)


class ModelLaneRoutingResult(ModelRoutingValue):
    status: Literal[
        "dispatched", "joined", "same-owner", "exception", "triage", "triage-cap"
    ]
    reason: str
    diagnosis: ModelOwnerDiagnosis | None = None
    key: str | None = None
    lane: str | None = None
    read_files: tuple[str, ...] = ()
    read_errors: tuple[str, ...] = ()
