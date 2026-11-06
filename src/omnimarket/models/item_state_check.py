# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared item snapshots, state findings and the immutable recovery specification."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelItemData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelItemLane(ModelItemData):
    lane: str = Field(min_length=1)
    outcome: str
    attempt: int = Field(ge=0)
    done: bool = False


class ModelItemPark(ModelItemData):
    notice: str = Field(min_length=1)
    authority_kind: Literal["RULING", "HOLD"]
    authority_stamp: datetime
    review_by: datetime


class ModelItemDelivery(ModelItemData):
    digest: str
    kind: Literal["reply", "queue", "question"]
    accepted: bool = False
    printed: bool = False
    tick_id: str = ""
    position: int | None = Field(default=None, ge=1)
    reason: Literal["capacity", "budget"] | None = None


class ModelItem(ModelItemData):
    item_id: str = Field(min_length=1)
    words: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    state: str
    owner: str
    session_id: str
    message_id: str
    repository: str = Field(min_length=1)
    memory_limit_mb: int = Field(gt=0)
    running_lane: str | None = None
    ended_lane: ModelItemLane | None = None
    park: ModelItemPark | None = None
    deliveries: tuple[ModelItemDelivery, ...] = ()
    age_minutes: float = Field(default=0, ge=0)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.words.encode("utf-8")).hexdigest()

    @property
    def family(self) -> str:
        return f"item@{self.item_id}@{self.digest}"


class ModelItemAuthority(ModelItemData):
    kind: Literal["RULING", "HOLD"]
    stamp: datetime


class ModelItemSnapshot(ModelItemData):
    items: tuple[ModelItem, ...]
    now: datetime
    tick_id: str = ""
    authorities: tuple[ModelItemAuthority, ...] = ()


class ModelItemRule(ModelItemData):
    rule_id: str
    class_key: str
    mode: Literal["shadow", "dispatch"]


class ModelItemPolicy(ModelItemData):
    closed_states: tuple[str, ...]
    parked_state: str
    operator_owner: str
    operator_action: str
    escalation_action: str
    actions: dict[str, str]
    rules: dict[str, ModelItemRule]
    retry_limit: int = Field(ge=0)
    done_outcomes: tuple[str, ...]


class ModelItemFinding(ModelItemData):
    item_id: str
    digest: str
    class_key: str
    rule_id: str
    label: str


class ModelItemFindings(ModelItemData):
    findings: tuple[ModelItemFinding, ...] = ()


class ModelRecoveryRecommendation(ModelItemData):
    item_id: str
    digest: str
    source: str
    repository: str | None = None
    memory_limit_mb: int | None = Field(default=None, gt=0)


class ModelItemRecoveryRequest(ModelItemData):
    item: ModelItem
    findings: tuple[ModelItemFinding, ...]
    recommendations: tuple[ModelRecoveryRecommendation, ...] = ()


class ModelItemRecoveryPlan(ModelItemData):
    item_id: str
    digest: str
    family: str
    cause: str
    action: str
    attempt: int = Field(ge=0)
    repository: str
    memory_limit_mb: int = Field(gt=0)
    brief: str
    escalation: bool = False
    rule_ids: tuple[str, ...]

    @property
    def claim_key(self) -> str:
        """The answer cause stays canonical; its retry is a fenced attempt."""
        return (
            f"{self.cause}#{self.attempt}"
            if self.cause.startswith("answer@")
            else self.cause
        )


class ModelItemPromotion(ModelItemData):
    """A dispatch decision emitted by the Part A measured promotion gate."""

    rule_id: str
    mode: Literal["shadow", "dispatch"]
    evidence: str = Field(min_length=1)


class ModelItemTickRequest(ModelItemData):
    snapshot: ModelItemSnapshot
    free_slots: int = Field(ge=0)
    recommendations: tuple[ModelRecoveryRecommendation, ...] = ()
    promotions: tuple[ModelItemPromotion, ...] = ()


class ModelItemDispatchReceipt(ModelItemData):
    cause: str
    lane: str | None = None
    accepted: bool = False
    printed: bool = False


class ModelItemClaim(ModelItemData):
    plan: ModelItemRecoveryPlan
    receipt: ModelItemDispatchReceipt | None = None


class ModelItemTickResult(ModelItemData):
    findings: tuple[ModelItemFinding, ...]
    started: tuple[ModelItemDispatchReceipt, ...] = ()
    queued: tuple[tuple[str, ModelItemDelivery], ...] = ()
    lines: tuple[str, ...] = ()
    snapshot: ModelItemSnapshot
    g2_unanswered: tuple[str, ...] = ()
