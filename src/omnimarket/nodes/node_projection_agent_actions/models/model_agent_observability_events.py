# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The six agent observability events, as the retired consumer validated them.

Each model keeps the field set, bounds and strictness of the model it replaces
(``extra`` forbid where that one forbade, ignore where it ignored). The two
events that carried a random ``id`` minted at ingestion declare it required
here: the handler derives it from the event, so a replay names the same row.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from uuid import UUID

from omnibase_core.types import JsonType
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

MAX_METADATA_SIZE_BYTES: int = 65_536
MAX_RAW_PAYLOAD_SIZE_BYTES: int = 1_048_576


def _bounded(
    value: dict[str, JsonType] | None, limit: int
) -> dict[str, JsonType] | None:
    """Replace an oversized object with a marker, as the retired models did."""
    if value is None:
        return None
    size = len(json.dumps(value).encode("utf-8"))
    if size > limit:
        logger.warning("Payload object exceeds %d bytes (%d), truncating", limit, size)
        return {"_truncated": True, "_original_size_bytes": size}
    return value


class ModelAgentAction(BaseModel):
    """onex.evt.omniclaude.agent-actions.v1 (table agent_actions)."""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    id: UUID
    created_at: datetime
    correlation_id: UUID
    agent_name: str
    action_type: str
    action_name: str
    status: str | None = None
    duration_ms: int | None = None
    result: str | None = None
    error_message: str | None = None
    metadata: dict[str, JsonType] | None = None
    raw_payload: dict[str, JsonType] | None = None
    project_path: str | None = None
    project_name: str | None = None
    working_directory: str | None = None

    @field_validator("metadata", mode="before")
    @classmethod
    def _bound_metadata(
        cls, v: dict[str, JsonType] | None
    ) -> dict[str, JsonType] | None:
        return _bounded(v, MAX_METADATA_SIZE_BYTES)

    @field_validator("raw_payload", mode="before")
    @classmethod
    def _bound_raw_payload(
        cls, v: dict[str, JsonType] | None
    ) -> dict[str, JsonType] | None:
        return _bounded(v, MAX_RAW_PAYLOAD_SIZE_BYTES)


class ModelTransformationEvent(BaseModel):
    """onex.evt.omniclaude.agent-transformation.v1 (table agent_transformation_events)."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    id: UUID
    correlation_id: UUID
    source_agent: str
    target_agent: str
    created_at: datetime
    trigger: str | None = None
    context: str | None = None
    metadata: dict[str, JsonType] | None = None
    raw_payload: dict[str, JsonType] | None = None
    project_path: str | None = None
    project_name: str | None = None
    claude_session_id: str | None = None


class ModelPerformanceMetric(BaseModel):
    """onex.evt.omniclaude.performance-metrics.v1 (table router_performance_metrics)."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    id: UUID
    metric_name: str
    metric_value: float
    created_at: datetime
    correlation_id: UUID | None = None
    unit: str | None = None
    agent_name: str | None = None
    labels: dict[str, str] | None = None
    metadata: dict[str, JsonType] | None = None
    raw_payload: dict[str, JsonType] | None = None


class ModelDetectionFailure(BaseModel):
    """onex.evt.omniclaude.detection-failure.v1 (table agent_detection_failures)."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID
    failure_reason: str
    created_at: datetime
    request_summary: str | None = None
    attempted_patterns: tuple[str, ...] | None = None
    fallback_used: str | None = None
    error_code: str | None = None
    metadata: dict[str, JsonType] | None = None
    raw_payload: dict[str, JsonType] | None = None
    project_path: str | None = None
    project_name: str | None = None
    claude_session_id: str | None = None


class ModelExecutionLog(BaseModel):
    """onex.evt.omniclaude.agent-execution-logs.v1 (table agent_execution_logs)."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    execution_id: UUID
    correlation_id: UUID
    agent_name: str
    status: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    error_message: str | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    metadata: dict[str, JsonType] | None = None
    raw_payload: dict[str, JsonType] | None = None
    project_path: str | None = None
    project_name: str | None = None
    claude_session_id: str | None = None
    terminal_id: str | None = None


class ModelAgentStatusEvent(BaseModel):
    """onex.evt.omniclaude.agent-status.v1 (table agent_status_events)."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    id: UUID
    correlation_id: UUID
    agent_name: str
    session_id: str
    state: str
    message: str
    created_at: datetime
    status_schema_version: int = 1
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    current_phase: str | None = None
    current_task: str | None = None
    blocking_reason: str | None = None
    metadata: dict[str, JsonType] | None = None
