# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The wire shape of ``onex.evt.omniclaude.hook-event.v1`` as this node reads it.

OMN-19513. The producer-side authority is the omniclaude capture contract
(``src/omniclaude/hooks/contracts/contract_hook_claude_capture.yaml`` and its
wire schema ``contracts/wire/hook_event_v1.yaml``). omnimarket does not depend
on omniclaude, so the consumer declares the shape it accepts here and the
fixture tests pin it to the producer's generated fixtures.

The per-hook payload is carried as a JSON object rather than as 33 payload
models, because this projection stores the payload verbatim in a JSONB column
and promotes only lineage to columns. What it does NOT do is accept any JSON
object: the payload is metadata-only by contract, so every key must be one the
contract's redaction table declares for the metadata topic. An undeclared key
is refused rather than stored. That is the consumer half of the rule that full
content never reaches this table: if a producer regression ever put a prompt or
a tool result on the metadata topic under a new key, the event is dead-lettered
instead of written.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from omnimarket.nodes.node_projection_claude_hook_events.models.enum_claude_hook_event_name import (
    EnumClaudeHookEventName,
)

#: The payload keys the capture contract declares for the metadata topic
#: (``redaction.fields`` entries under ``payload.``). Pinned to the omniclaude
#: contract by ``tests/test_omn19513_claude_hook_events_projection.py``.
ALLOWED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "action",
        "agent_id",
        "agent_transcript_ref",
        "agent_type",
        "background_task_count",
        "command_args_ref",
        "command_name",
        "command_source",
        "compact_summary_ref",
        "content_ref",
        "context_tokens",
        "custom_instructions_ref",
        "delta_length",
        "delta_ref",
        "directory_ref",
        "duration_ms",
        "elicitation_id",
        "error",
        "error_details_ref",
        "error_ref",
        "event",
        "expansion_type",
        "file_path_ref",
        "final",
        "glob_count",
        "hook_event_name",
        "index",
        "interrupted",
        "is_interrupt",
        "last_assistant_message_ref",
        "load_reason",
        "mcp_server",
        "mcp_server_name",
        "memory_type",
        "message_id",
        "message_ref",
        "mode",
        "model",
        "name_ref",
        "new_cwd_ref",
        "notification_type",
        "old_cwd_ref",
        "prompt_cache_likely_expired",
        "prompt_length",
        "prompt_ref",
        "reason",
        "reason_ref",
        "requested_schema_keys",
        "seconds_since_last_response",
        "session_cron_count",
        "session_title_ref",
        "source",
        "stop_hook_active",
        "suggestion_count",
        "task_description_ref",
        "task_id",
        "task_subject_ref",
        "team_name",
        "teammate_name",
        "title_ref",
        "tool_call_key",
        "tool_input_keys",
        "tool_input_ref",
        "tool_name",
        "tool_names",
        "tool_response_ref",
        "tool_use_ids",
        "trigger",
        "turn_id",
        "worktree_path_ref",
    }
)

_REF_SUFFIX = "_ref"

#: Top-level keys the local emit seam stamps onto every record it publishes,
#: which the capture contract's event does not declare. The emit daemon adds
#: ``correlation_id``, ``causation_id``, ``entity_id`` and (from its own
#: process environment) ``session_id`` when absent, and the governed redaction
#: stamps ``redaction_state`` on every record it passes. Measured on the lab
#: bus 2026-09-26: a live ``tool-executed.v1`` record carries all five at top
#: level. Each duplicates or describes what the event states in ``lineage``,
#: so they are dropped, by name -- never by a blanket "ignore extras", which
#: would also admit a content-bearing key.
TRANSPORT_STAMP_KEYS: frozenset[str] = frozenset(
    {"correlation_id", "causation_id", "entity_id", "session_id", "redaction_state"}
)


class ModelClaudeHookContentRefWire(BaseModel):
    """A pointer to one restricted content record: never the content itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    length: int = Field(ge=0)
    content_record_id: UUID


class ModelClaudeHookLineageWire(BaseModel):
    """Who fired the hook, inside which agent, under which call.

    ``agent_id`` is the only discriminator between a subagent's events and the
    main thread's: subagents share their parent's ``session_id`` (measured on
    the lab, 2026-09-26). A null ``agent_id`` means the main thread.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    agent_id: str | None
    agent_type: str | None
    is_subagent: bool
    parent_tool_use_id: str | None
    workflow_run_id: str | None
    spawn_depth: int | None = Field(ge=0)
    tool_use_id: str | None
    prompt_id: str | None
    turn_id: str | None
    correlation_id: UUID
    causation_id: UUID | None

    @model_validator(mode="after")
    def _subagent_flag_matches_agent_id(self) -> ModelClaudeHookLineageWire:
        if self.is_subagent != (self.agent_id is not None):
            raise ValueError("is_subagent must equal (agent_id is not None)")
        return self


class ModelClaudeHookEventWire(BaseModel):
    """One captured hook event, metadata only, as published on the bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    schema_version: Literal["1.0.0"]
    hook_event_name: EnumClaudeHookEventName
    emitted_at: AwareDatetime
    actor: Literal["claude"]
    claude_code_version: str | None = None
    lineage: ModelClaudeHookLineageWire
    payload: dict[str, JsonValue]
    content_refs: tuple[ModelClaudeHookContentRefWire, ...]

    @model_validator(mode="before")
    @classmethod
    def _drop_transport_keys(cls, data: Any) -> Any:
        """Strip what the transport added; keep everything the event states.

        The shared runtime injects ``_db``, ``_topic``, ``_event_type``,
        ``_partition``, ``_offset``, ``_envelope_id``, ``_envelope_timestamp``
        and ``_tenant_id``; the standalone unwrapper adds ``_envelope``. No event
        field starts with an underscore, so the prefix is an exact
        discriminator. The emit seam's stamps are dropped by name
        (``TRANSPORT_STAMP_KEYS``).
        """
        if not isinstance(data, dict):
            return data
        return {
            k: v
            for k, v in data.items()
            if not str(k).startswith("_") and k not in TRANSPORT_STAMP_KEYS
        }

    @model_validator(mode="after")
    def _payload_is_declared_metadata(self) -> ModelClaudeHookEventWire:
        undeclared = sorted(set(self.payload) - ALLOWED_PAYLOAD_KEYS)
        if undeclared:
            raise ValueError(
                "payload carries keys the capture contract does not declare for "
                f"the metadata topic: {undeclared}; refusing rather than storing "
                "possible content"
            )
        if self.payload.get("hook_event_name") != self.hook_event_name.value:
            raise ValueError(
                "payload.hook_event_name must equal the event's hook_event_name"
            )
        tool_name = self.payload.get("tool_name")
        if tool_name is not None and not isinstance(tool_name, str):
            raise ValueError("payload.tool_name must be a string or null")

        payload_refs: list[ModelClaudeHookContentRefWire] = []
        for key, value in self.payload.items():
            if not key.endswith(_REF_SUFFIX) or value is None:
                continue
            payload_refs.append(ModelClaudeHookContentRefWire.model_validate(value))
        declared_ids = [ref.content_record_id for ref in self.content_refs]
        if len(declared_ids) != len(set(declared_ids)):
            raise ValueError("content_refs lists a content record more than once")
        if set(payload_refs) != set(self.content_refs):
            raise ValueError(
                "content_refs must list exactly the content references the payload uses"
            )
        return self

    @property
    def tool_name(self) -> str | None:
        value = self.payload.get("tool_name")
        return value if isinstance(value, str) else None


__all__ = [
    "ALLOWED_PAYLOAD_KEYS",
    "TRANSPORT_STAMP_KEYS",
    "ModelClaudeHookContentRefWire",
    "ModelClaudeHookEventWire",
    "ModelClaudeHookLineageWire",
]
