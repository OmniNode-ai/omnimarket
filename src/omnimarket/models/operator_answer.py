# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared wire boundary for answer dispatch, leases and durable delivery evidence."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelAnswerBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelAnswerBrief(ModelAnswerBase):
    kind: Literal["answer"] = "answer"
    headless: Literal[True] = True
    tools: tuple[
        Literal[
            "read_item",
            "read_ledger",
            "read_live_state",
            "check_operator_reply",
            "deliver_answer",
        ],
        ...,
    ]
    instructions: str


class ModelAnswerConfig(ModelAnswerBase):
    dispatch_age_minutes: float = Field(gt=0)
    tick_interval_minutes: float = Field(gt=0)
    lane_deadline_minutes: float = Field(gt=0)
    replacement_deadline_minutes: float = Field(gt=0)
    max_age_minutes: float = Field(gt=0)
    answer_key_prefix: str = Field(min_length=1)
    effect_prefix: str = Field(min_length=1)
    category: str = Field(min_length=1)
    miss_category: str = Field(min_length=1)
    alert_timeout_seconds: float = Field(gt=0)
    brief: ModelAnswerBrief


class ModelAnswerDeliveryEvidence(ModelAnswerBase):
    kind: Literal["delivered"] = "delivered"
    at: AwareDatetime
    channel: Literal["alert", "prompt"]
    receipt: str = Field(min_length=1)
    effect_id: str = Field(min_length=1)


class ModelAnswerMessage(ModelAnswerBase):
    message_id: str = Field(min_length=1)
    answer_key: str = Field(min_length=1)
    arrived_at: AwareDatetime
    words: str = Field(min_length=1)
    delivery_evidence: ModelAnswerDeliveryEvidence | None = None
    is_ask: bool = False
    work_key: str | None = None

    @property
    def answered(self) -> bool:
        return self.delivery_evidence is not None


class ModelAnswerContext(ModelAnswerBase):
    message: ModelAnswerMessage
    item: str = Field(min_length=1)
    ledger: tuple[str, ...]
    live_state: str = Field(min_length=1)


class ModelAnswerLease(ModelAnswerBase):
    key: str
    attempt: int = Field(ge=1, le=2)
    deadline: AwareDatetime
    token: str = Field(min_length=1)
    brief: ModelAnswerBrief


class ModelAnswerSendReceipt(ModelAnswerBase):
    accepted: bool
    receipt: str = ""
    detail: str = ""


class ModelAnswerRow(ModelAnswerBase):
    row_id: str
    at: AwareDatetime
    message_id: str
    answer_key: str
    effect_id: str
    kind: Literal[
        "reserved",
        "started",
        "intended",
        "MSG",
        "delivered",
        "miss",
        "UNREAD",
        "printed",
    ]
    attempt: int = Field(ge=1, le=2)
    text: str = ""
    channel: Literal["alert", "prompt", "none"] = "none"
    receipt: str = ""

    def ledger_text(self) -> str:
        """Queued MSG is distinct from accepted/printed delivery, even after mirroring."""
        stamp = self.at.isoformat().replace("+00:00", "Z")
        row_type = "MSG" if self.kind == "MSG" else "STATUS"
        text = " ".join(self.text.replace("|", "/").split())
        return (
            f"{stamp} | {row_type} | lane={self.answer_key} | to=operator"
            f" | answers={self.message_id} | kind=answer-{self.kind}"
            f" | effect_id={self.effect_id} | attempt={self.attempt}"
            f" | channel={self.channel} | receipt={self.receipt} | {text}"
        )


class ModelAnswerState(ModelAnswerBase):
    message: ModelAnswerMessage
    lease: ModelAnswerLease
    effect_id: str
    text: str = ""
    sends: int = Field(default=0, ge=0, le=2)
    delivered_at: AwareDatetime | None = None
    delivered_receipt: str = ""
    printed: bool = False
    miss_notified: bool = False
    unread: str = ""


class ModelAnswerRequest(ModelAnswerBase):
    correlation_id: UUID = Field(default_factory=uuid4)
    operation: Literal["tick", "answer", "prompt", "goal_check"]
    now: AwareDatetime
    answer_key: str = ""
    lease_token: str = ""
    session_id: str = ""


class ModelAnswerResult(ModelAnswerBase):
    correlation_id: UUID
    started: tuple[str, ...] = ()
    delivered: tuple[str, ...] = ()
    printed: tuple[str, ...] = ()
    unread: tuple[str, ...] = ()
    overdue: tuple[str, ...] = ()
    work_needed: tuple[str, ...] = ()
