# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types for R-B and the revision-five batch probe. Validation belongs to core."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelBriefPremise(_Model):
    kind: Literal["pr", "ticket", "head"]
    reference: str
    repository: str = ""


class ModelBriefCheckRequest(_Model):
    brief: str
    lane: str
    ticket: str
    host: str
    repository: str = ""
    now: float
    batch: str = ""
    brief_id: str = ""
    premise: str = ""
    work_waiting: bool = True


class ModelBriefCheckResult(_Model):
    verdict: Literal["pass", "refuse", "wait", "UNREAD"]
    receipt: str = ""
    reason: str = ""
    install_rows: tuple[str, ...] = ()
    premises: tuple[ModelBriefPremise, ...] = ()
    slot: str = ""
    attempt: int = 0


class ModelPremiseRead(_Model):
    """A live source read, never a lane's self-reported checkpoint."""

    premise: ModelBriefPremise
    readable: bool
    merged: bool = False
    head: str = ""
    installed_revision: str = ""
    contained: bool = False
    install_row: str = ""
    ticket_state: str = ""


class ModelPremiseRuleSource(_Model):
    stamp: str
    lane: str


class ModelPremiseRule(_Model):
    id: str
    source_ruling: ModelPremiseRuleSource
    dispatch_kinds: tuple[str, ...]
    check: str
    mode: Literal["route", "refuse"]
    fix: str


class ModelPremiseValidationRequest(_Model):
    rule: ModelPremiseRule
    request: ModelBriefCheckRequest
    premises: tuple[ModelBriefPremise, ...]
    reads: tuple[ModelPremiseRead, ...]


class ModelBatchPolicy(_Model):
    probe: int = Field(ge=1)
    blocked_threshold: int = Field(ge=1)
    window_s: float = Field(gt=0)
    step: int = Field(ge=1)
    lease_s: float = Field(gt=0)
    max_attempts: int = Field(ge=1, le=2)
    park_after: int = Field(ge=1)
    park_s: float = Field(gt=0)


class ModelBatchRequest(_Model):
    operation: Literal["admit", "blocked", "proof", "checkpoint", "timeout", "crashed"]
    batch: str
    brief_id: str
    premise: str
    now: float
    work_waiting: bool = True
    slot: str = ""
    attempt: int = 0
    class_key: str = ""
    receipt_id: str = ""


class ModelBatchSlot(_Model):
    key: str
    brief_id: str
    premise: str
    attempt: int
    lease_until: float
    state: Literal["running", "retry", "blocked", "expired", "timed-out", "proved"]
    class_key: str = ""
    blocked_at: float = 0


class ModelBatchState(_Model):
    batch: str
    policy: ModelBatchPolicy
    slots: tuple[ModelBatchSlot, ...] = ()
    proofs: tuple[str, ...] = ()
    held: str = ""
    escalation: str = ""


class ModelBatchResult(_Model):
    verdict: Literal["admit", "wait", "held", "recorded", "refuse", "UNREAD"]
    slot: str = ""
    attempt: int = 0
    reason: str = ""
    escalation: str = ""
    capacity: int = 0


class ModelSendRefusal(_Model):
    ticket: str
    reason: str
    attempts: int = 0
    parked_until: float = 0
