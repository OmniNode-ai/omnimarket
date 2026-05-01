"""Typed envelopes for Pattern B cross-CLI broker handoff."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumPatternBBrokerEventType(StrEnum):
    dispatch_requested = "dispatch_requested"
    dispatch_published = "dispatch_published"
    terminal_completed = "terminal_completed"
    terminal_failed = "terminal_failed"
    terminal_timed_out = "terminal_timed_out"


class EnumPatternBBrokerState(StrEnum):
    accepted = "accepted"
    denied = "denied"
    published = "published"
    completed = "completed"
    failed = "failed"
    timed_out = "timed_out"


class EnumPatternBBrokerTerminalStatus(StrEnum):
    completed = "completed"
    failed = "failed"
    timed_out = "timed_out"


class EnumPatternBBrokerAclDecision(StrEnum):
    allow = "allow"
    deny = "deny"


class EnumPatternBBrokerOriginator(StrEnum):
    omnimarket = "omnimarket"
    omnicodex = "omnicodex"
    omniclaude = "omniclaude"
    opencode = "opencode"


class EnumPatternBBrokerRecipient(StrEnum):
    omnicodex = "omnicodex"
    omniclaude = "omniclaude"
    opencode = "opencode"


class ModelPatternBBrokerWaitPolicy(BaseModel):
    """Contract for synchronous callers waiting on terminal broker events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    wait_for_terminal_event: bool = False
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    terminal_statuses: tuple[EnumPatternBBrokerTerminalStatus, ...] = (
        EnumPatternBBrokerTerminalStatus.completed,
        EnumPatternBBrokerTerminalStatus.failed,
        EnumPatternBBrokerTerminalStatus.timed_out,
    )

    @field_validator("terminal_statuses")
    @classmethod
    def validate_terminal_statuses(
        cls,
        value: tuple[EnumPatternBBrokerTerminalStatus, ...],
    ) -> tuple[EnumPatternBBrokerTerminalStatus, ...]:
        if not value:
            raise ValueError("terminal_statuses must contain at least one status")
        return value


class ModelPatternBBrokerTopicBindings(BaseModel):
    """Topic bindings loaded from the node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatch_request_topic: str
    terminal_completed_topic: str
    terminal_failed_topic: str

    @field_validator("*")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("topic must be a non-empty string")
        return normalized


class ModelPatternBBrokerRuntimeConfig(BaseModel):
    """Contract-derived runtime settings for broker handlers/adapters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topics: ModelPatternBBrokerTopicBindings
    consumer_group: str
    default_wait_policy: ModelPatternBBrokerWaitPolicy = Field(
        default_factory=ModelPatternBBrokerWaitPolicy
    )
    allowed_originators: tuple[EnumPatternBBrokerOriginator, ...]
    allowed_recipients: tuple[EnumPatternBBrokerRecipient, ...]

    @field_validator("consumer_group")
    @classmethod
    def validate_consumer_group(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("consumer_group must be a non-empty string")
        return normalized

    @field_validator("allowed_originators")
    @classmethod
    def validate_allowed_originators(
        cls,
        value: tuple[EnumPatternBBrokerOriginator, ...],
    ) -> tuple[EnumPatternBBrokerOriginator, ...]:
        if not value:
            raise ValueError("allowed_originators must contain at least one value")
        return value

    @field_validator("allowed_recipients")
    @classmethod
    def validate_allowed_recipients(
        cls,
        value: tuple[EnumPatternBBrokerRecipient, ...],
    ) -> tuple[EnumPatternBBrokerRecipient, ...]:
        if not value:
            raise ValueError("allowed_recipients must contain at least one value")
        return value


class ModelPatternBBrokerAclInput(BaseModel):
    """Inputs used by a broker ACL handler before a dispatch is published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    originator: EnumPatternBBrokerOriginator
    recipient: EnumPatternBBrokerRecipient
    skill_name: str = Field(min_length=1, max_length=128)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))

    @field_validator("skill_name")
    @classmethod
    def validate_skill_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("skill_name must be a non-empty string")
        return normalized


class ModelPatternBBrokerAclResult(BaseModel):
    """ACL result emitted by the broker before publish."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: EnumPatternBBrokerAclDecision
    reason: str = Field(min_length=1, max_length=512)
    matched_rule: str | None = Field(default=None, max_length=128)


class ModelPatternBBrokerDispatchRequest(BaseModel):
    """Dispatch request accepted by the Pattern B broker publish adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID
    event_type: EnumPatternBBrokerEventType = (
        EnumPatternBBrokerEventType.dispatch_requested
    )
    state: EnumPatternBBrokerState = EnumPatternBBrokerState.accepted
    originator: EnumPatternBBrokerOriginator
    recipient: EnumPatternBBrokerRecipient
    skill_name: str = Field(min_length=1, max_length=128)
    payload: Mapping[str, Any] = Field(default_factory=dict)
    wait_policy: ModelPatternBBrokerWaitPolicy = Field(
        default_factory=ModelPatternBBrokerWaitPolicy
    )
    requested_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))

    @field_validator("event_type")
    @classmethod
    def validate_dispatch_event_type(
        cls,
        value: EnumPatternBBrokerEventType,
    ) -> EnumPatternBBrokerEventType:
        if value is not EnumPatternBBrokerEventType.dispatch_requested:
            raise ValueError("dispatch request event_type must be dispatch_requested")
        return value

    @field_validator("state")
    @classmethod
    def validate_dispatch_state(
        cls,
        value: EnumPatternBBrokerState,
    ) -> EnumPatternBBrokerState:
        if value not in {
            EnumPatternBBrokerState.accepted,
            EnumPatternBBrokerState.denied,
        }:
            raise ValueError("dispatch request state must be accepted or denied")
        return value

    @field_validator("skill_name")
    @classmethod
    def validate_skill_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("skill_name must be a non-empty string")
        return normalized


class ModelPatternBBrokerTerminalEvent(BaseModel):
    """Terminal event consumed by the Pattern B broker wait adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    correlation_id: UUID
    event_type: EnumPatternBBrokerEventType
    state: EnumPatternBBrokerState
    status: EnumPatternBBrokerTerminalStatus
    result: Mapping[str, Any] = Field(default_factory=dict)
    error_message: str | None = Field(default=None, max_length=2048)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))

    @field_validator("event_type")
    @classmethod
    def validate_terminal_event_type(
        cls,
        value: EnumPatternBBrokerEventType,
    ) -> EnumPatternBBrokerEventType:
        if value not in {
            EnumPatternBBrokerEventType.terminal_completed,
            EnumPatternBBrokerEventType.terminal_failed,
            EnumPatternBBrokerEventType.terminal_timed_out,
        }:
            raise ValueError("terminal event_type must be a terminal enum value")
        return value

    @field_validator("state")
    @classmethod
    def validate_terminal_state(
        cls,
        value: EnumPatternBBrokerState,
    ) -> EnumPatternBBrokerState:
        if value not in {
            EnumPatternBBrokerState.completed,
            EnumPatternBBrokerState.failed,
            EnumPatternBBrokerState.timed_out,
        }:
            raise ValueError("terminal state must be completed, failed, or timed_out")
        return value

    @model_validator(mode="after")
    def validate_terminal_consistency(self) -> ModelPatternBBrokerTerminalEvent:
        expected_state = {
            EnumPatternBBrokerEventType.terminal_completed: (
                EnumPatternBBrokerState.completed,
                EnumPatternBBrokerTerminalStatus.completed,
            ),
            EnumPatternBBrokerEventType.terminal_failed: (
                EnumPatternBBrokerState.failed,
                EnumPatternBBrokerTerminalStatus.failed,
            ),
            EnumPatternBBrokerEventType.terminal_timed_out: (
                EnumPatternBBrokerState.timed_out,
                EnumPatternBBrokerTerminalStatus.timed_out,
            ),
        }[self.event_type]
        if (self.state, self.status) != expected_state:
            raise ValueError(
                "terminal event_type, state, and status must describe the same outcome"
            )
        return self


class ModelPatternBBrokerPublishReceipt(BaseModel):
    """Receipt returned after a Pattern B dispatch request is published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    correlation_id: UUID
    event_type: EnumPatternBBrokerEventType = (
        EnumPatternBBrokerEventType.dispatch_published
    )
    state: EnumPatternBBrokerState = EnumPatternBBrokerState.published
    topic: str = Field(min_length=1, max_length=512)
    key: str = Field(min_length=1, max_length=256)
    payload_size_bytes: int = Field(ge=1)
    wait_policy: ModelPatternBBrokerWaitPolicy
    published_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))

    @field_validator("event_type")
    @classmethod
    def validate_publish_event_type(
        cls,
        value: EnumPatternBBrokerEventType,
    ) -> EnumPatternBBrokerEventType:
        if value is not EnumPatternBBrokerEventType.dispatch_published:
            raise ValueError("publish receipt event_type must be dispatch_published")
        return value

    @field_validator("state")
    @classmethod
    def validate_publish_state(
        cls,
        value: EnumPatternBBrokerState,
    ) -> EnumPatternBBrokerState:
        if value is not EnumPatternBBrokerState.published:
            raise ValueError("publish receipt state must be published")
        return value

    @field_validator("topic", "key")
    @classmethod
    def validate_publish_metadata(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("publish metadata must be a non-empty string")
        return normalized


__all__ = [
    "EnumPatternBBrokerAclDecision",
    "EnumPatternBBrokerEventType",
    "EnumPatternBBrokerOriginator",
    "EnumPatternBBrokerRecipient",
    "EnumPatternBBrokerState",
    "EnumPatternBBrokerTerminalStatus",
    "ModelPatternBBrokerAclInput",
    "ModelPatternBBrokerAclResult",
    "ModelPatternBBrokerDispatchRequest",
    "ModelPatternBBrokerPublishReceipt",
    "ModelPatternBBrokerRuntimeConfig",
    "ModelPatternBBrokerTerminalEvent",
    "ModelPatternBBrokerTopicBindings",
    "ModelPatternBBrokerWaitPolicy",
]
