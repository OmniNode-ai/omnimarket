# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-owned hook event shape; tests pin compatibility to the consumer wire."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue


class ModelBeaconHookLineage(BaseModel):
    """Codex session identity, without content or account identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    agent_id: str | None = None
    agent_type: str | None = None
    is_subagent: bool = False
    parent_tool_use_id: str | None = None
    workflow_run_id: str | None = None
    spawn_depth: int | None = Field(default=None, ge=0)
    tool_use_id: str | None = None
    prompt_id: str | None = None
    turn_id: str | None = None
    correlation_id: UUID
    causation_id: UUID | None = None


class ModelBeaconHookEvent(BaseModel):
    """Metadata-only hook event, with the harness carried by actor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    schema_version: Literal["1.0.0"] = "1.0.0"
    hook_event_name: str
    emitted_at: AwareDatetime
    actor: Literal["codex"] = "codex"
    host: str | None = Field(default=None, min_length=1)
    lane: str | None = None
    lineage: ModelBeaconHookLineage
    payload: dict[str, JsonValue]
    content_refs: tuple[()] = ()
