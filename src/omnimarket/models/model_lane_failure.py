# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt projections and recovery-plan contributions shared across bus nodes.

The rule document is opaque here: only core admits/validates error-rule rows.
This package consumes an admitted row's identity and action, not a second schema.
The shared recovery-plan consumer owns claims, replacement starts and escalation.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from omnimarket.models.error_normalizer import ModelErrorNormalizerFacts


class ModelLaneFailureValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelLaneReceiptGuardEnd(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    scope_result: str = ""


class ModelLaneReceiptLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    mem_bytes: int = Field(default=0, ge=0)


class ModelLaneReceiptGuard(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    end: ModelLaneReceiptGuardEnd = Field(default_factory=ModelLaneReceiptGuardEnd)
    requested: ModelLaneReceiptLimits = Field(default_factory=ModelLaneReceiptLimits)


class ModelLaneReceiptRetention(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    error: str = ""
    reading: str = ""
    rc: int | None = None


class ModelLaneReceiptWorkdir(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    tree: str
    disposition: str


class ModelLaneReceiptRepository(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    repo: str
    sha: str = ""


class ModelLaneFailureReceipt(BaseModel):
    """Projection of the runner's receipt; unrelated fields remain runner-owned."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    lane: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    host: str = ""
    ticket: str = ""
    started_at: str = ""
    engine_rc: int | None = None
    guard: ModelLaneReceiptGuard = Field(default_factory=ModelLaneReceiptGuard)
    retention: ModelLaneReceiptRetention = Field(
        default_factory=ModelLaneReceiptRetention
    )
    repo: ModelLaneReceiptRepository | None = None
    workdir: ModelLaneReceiptWorkdir | str | None = None


class ModelLaneFailureRequest(ModelLaneFailureValue):
    receipt: ModelLaneFailureReceipt
    # Supplied by the shared attempt-family owner, never inferred from lane names.
    attempt_family: str = Field(min_length=1)
    original_lane: str = Field(min_length=1)
    original_brief: str = Field(min_length=1)
    for_items: tuple[str, ...] = Field(min_length=1)
    replacement_index: int = Field(default=0, ge=0)
    # Repository diagnosis is a second input to the SAME recovery plan.
    target_repository: str | None = None


class ModelLaneMemoryStep(ModelLaneFailureValue):
    mem_bytes: int = Field(gt=0)
    mem_max: str = Field(min_length=1)


class ModelLaneFailureSelector(ModelLaneFailureValue):
    rule_id: str
    signal: Literal["oom", "retention"]
    scope_result: str | None = None
    error_pattern: str | None = None
    canonical_text: str


class ModelLaneFailureEndpoint(ModelLaneFailureValue):
    node: str
    command_topic: str
    terminal_topic: str


class ModelLaneFailureBindings(ModelLaneFailureValue):
    normalizer: ModelLaneFailureEndpoint
    owner: ModelLaneFailureEndpoint
    admission: ModelLaneFailureEndpoint
    recovery: ModelLaneFailureEndpoint
    timeout_seconds: int = Field(gt=0)


class ModelLaneFailureOverlay(ModelLaneFailureValue):
    source: str
    producer: str
    normalizer_facts: ModelErrorNormalizerFacts
    normalizer_key_version: str
    rule_key_version: int = Field(gt=0)
    # Core owns the schema of this document and its authorization/measurements.
    error_rule_document: dict[str, JsonValue]
    selectors: tuple[ModelLaneFailureSelector, ...] = Field(min_length=1)
    memory_steps: tuple[ModelLaneMemoryStep, ...] = Field(min_length=1)
    max_mem_bytes: int = Field(gt=0)
    max_replacements: int = Field(ge=1)
    kept_workdir_disposition: str
    owner_map: dict[str, JsonValue]
    runner_path: str
    runner_report: str
    bindings: ModelLaneFailureBindings


class ModelLaneRetentionInstallRecord(ModelLaneFailureValue):
    installed_at: datetime
    host: str
    revision: str


class ModelLaneRetentionVerifyOverlay(ModelLaneFailureValue):
    check_after_seconds: tuple[int, ...] = Field(min_length=1)
    error_pattern: str
    receipt_glob: str


class ModelLaneRetentionVerifyRequest(ModelLaneFailureValue):
    receipt_directory: str
    install_record: str
    now: datetime
    check_after_seconds: int = Field(gt=0)


class ModelLaneRetentionVerifyResult(ModelLaneFailureValue):
    status: Literal["not-due", "unmeasured", "clear", "failed"]
    installed: ModelLaneRetentionInstallRecord
    checked_receipts: int
    violating_receipts: tuple[str, ...] = ()
    unreadable_receipts: tuple[str, ...] = ()


class ModelLaneFailureAdmissionRequest(ModelLaneFailureValue):
    document: dict[str, JsonValue]
    rule_id: str
    class_key: str
    key_version: int = Field(gt=0)
    normalizer_key_version: str
    scope_result: str
    repository: str


class ModelLaneFailureAdmission(ModelLaneFailureValue):
    allowed: bool
    reason: str = ""
    rule_id: str
    class_key: str
    mode: Literal["shadow", "hint", "dispatch"]
    action: Literal["fill-backlog-item"]


class ModelLaneFailureOwnerRequest(ModelLaneFailureValue):
    """Wire-compatible with the ownership compute node's public request."""

    blocker: str
    report_text: str
    owner_map: dict[str, JsonValue]
    files: tuple[dict[str, str], ...] = ()


class ModelLaneFailureOwnerReply(ModelLaneFailureValue):
    owner: str | None = None
    confidence: float = 0
    evidence_line: str = ""
    reason: str = ""


class ModelLaneRecoveryContribution(ModelLaneFailureValue):
    """One input to the shared plan, NEVER an independent runner start command.

    The consumer merges memory/repository changes before claiming the attempt
    family. Retention work instead uses its class key across all originating lanes.
    A replay with this key must return the original claim, including after timeout.
    """

    contract: Literal["fill-backlog-item"] = "fill-backlog-item"
    operation: Literal["replace", "runner-fix", "escalate"]
    key: str
    attempt_family: str
    original_lane: str
    class_key: str
    rule_id: str
    target_repo: str
    for_items: tuple[str, ...]
    brief: str
    receipt_run_id: str
    evidence: ModelLaneFailureReceipt
    mem_max: str | None = None
    mem_bytes: int | None = None
    salvage_worktree: str | None = None
    salvage_host: str | None = None
    replacement_limit: int


class ModelLaneRecoveryAck(ModelLaneFailureValue):
    key: str
    status: Literal["claimed", "joined", "escalated", "pending"]
    lane: str | None = None


class ModelLaneFailureMatch(ModelLaneFailureValue):
    rule_id: str
    class_key: str
    key_version: str
    status: Literal[
        "shadow", "hint", "refused", "claimed", "joined", "escalated", "pending"
    ]
    reason: str = ""
    contribution: ModelLaneRecoveryContribution | None = None
    lane: str | None = None


class ModelLaneFailureResult(ModelLaneFailureValue):
    matches: tuple[ModelLaneFailureMatch, ...]
