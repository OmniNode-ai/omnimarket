# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime delegation dispatch config loaded from the node contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelRuntimeDelegationDispatchTopics(BaseModel):
    """Downstream delegation runtime topic bindings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    completed: str
    failed: str

    @field_validator("*")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("topic must be a non-empty string")
        return normalized


class ModelRuntimeDelegationDispatchConfig(BaseModel):
    """Contract-derived settings for RuntimeDelegationDispatchPort."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topics: ModelRuntimeDelegationDispatchTopics
    request_message_type: str = Field(min_length=1)
    source_tool: str = Field(min_length=1)
    consumer_group_prefix: str = Field(min_length=1)
    wait_timeout_seconds: int = Field(ge=1, le=3600)

    @field_validator(
        "request_message_type",
        "source_tool",
        "consumer_group_prefix",
    )
    @classmethod
    def validate_non_empty_string(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must be a non-empty string")
        return normalized


__all__ = [
    "ModelRuntimeDelegationDispatchConfig",
    "ModelRuntimeDelegationDispatchTopics",
]
