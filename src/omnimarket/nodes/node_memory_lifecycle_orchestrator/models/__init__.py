# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Typed contract models for memory lifecycle orchestration."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers import (
    ModelArchiveMemoryCommand,
    ModelExpireMemoryCommand,
    ModelMemoryArchiveResult,
    ModelMemoryExpireResult,
    ModelMemoryTickResult,
)


class EnumLifecycleOrchestratorOperation(StrEnum):
    """Operations routed by node_memory_lifecycle_orchestrator."""

    TICK = "tick"
    EXPIRE = "expire"
    ARCHIVE = "archive"


class EnumLifecycleOrchestratorStatus(StrEnum):
    """Terminal status for a lifecycle orchestration attempt."""

    COMPLETED = "completed"
    NOOP = "noop"
    FAILED = "failed"


class ModelLifecycleOrchestratorInput(BaseModel):
    """Runtime dispatch envelope for memory lifecycle operations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumLifecycleOrchestratorOperation
    correlation_id: UUID
    runtime_tick: ModelRuntimeTick | None = None
    expire_command: ModelExpireMemoryCommand | None = None
    archive_command: ModelArchiveMemoryCommand | None = None

    @model_validator(mode="after")
    def require_matching_payload(self) -> ModelLifecycleOrchestratorInput:
        payloads = {
            "runtime_tick": self.runtime_tick,
            "expire_command": self.expire_command,
            "archive_command": self.archive_command,
        }
        required_by_operation = {
            EnumLifecycleOrchestratorOperation.TICK: "runtime_tick",
            EnumLifecycleOrchestratorOperation.EXPIRE: "expire_command",
            EnumLifecycleOrchestratorOperation.ARCHIVE: "archive_command",
        }
        required = required_by_operation[self.operation]
        if payloads[required] is None:
            raise ValueError(f"{self.operation.value} operation requires {required}")

        extras = [
            name
            for name, value in payloads.items()
            if name != required and value is not None
        ]
        if extras:
            raise ValueError(
                f"{self.operation.value} operation does not allow: {', '.join(extras)}"
            )
        return self


class ModelLifecycleOrchestratorOutput(BaseModel):
    """Typed result envelope emitted by memory lifecycle orchestration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumLifecycleOrchestratorStatus
    operation: EnumLifecycleOrchestratorOperation
    correlation_id: UUID
    emitted_event_count: int = Field(default=0, ge=0)
    processed_memory_count: int = Field(default=0, ge=0)
    tick_result: ModelMemoryTickResult | None = None
    expire_result: ModelMemoryExpireResult | None = None
    archive_result: ModelMemoryArchiveResult | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def require_error_message_for_failed_status(
        self,
    ) -> ModelLifecycleOrchestratorOutput:
        if (
            self.status == EnumLifecycleOrchestratorStatus.FAILED
            and not self.error_message
        ):
            raise ValueError("failed status requires error_message")
        return self


__all__: list[str] = [
    "EnumLifecycleOrchestratorOperation",
    "EnumLifecycleOrchestratorStatus",
    "ModelLifecycleOrchestratorInput",
    "ModelLifecycleOrchestratorOutput",
]
