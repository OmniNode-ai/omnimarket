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
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
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
        "exit_code",
        "expansion_type",
        "file_path_ref",
        "final",
        "glob_count",
        "goal_id",
        "guard",
        "hook_event_name",
        "index",
        "input_tokens",
        "interrupted",
        "is_interrupt",
        "last_assistant_message_ref",
        "load_reason",
        "mcp_server",
        "mcp_server_name",
        "memory_type",
        "message_count",
        "message_id",
        "message_ref",
        "mode",
        "model",
        "name_ref",
        "new_cwd_ref",
        "notification_type",
        "parent_goal_id",
        "old_cwd_ref",
        "output_tokens",
        "partial_window",
        "prompt_cache_likely_expired",
        "prompt_length",
        "prompt_ref",
        "reason",
        "reason_class",
        "reason_ref",
        "refused_hook_event_name",
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
        "usage_source_hook",
        "worktree_path_ref",
    }
)

_REF_SUFFIX = "_ref"

#: The counts an ``AssistantUsage`` payload must state (OMN-18946).
_USAGE_COUNT_KEYS = (
    "message_count",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

#: Top-level keys emit seams stamp onto every record, which the capture
#: contract's event does not declare. The appender adds ``lane`` (kept: see
#: ``ModelClaudeHookEventWire.lane``), ``lane_source``, ``lane_ticket``,
#: ``workspace_path``, and ``turn_id``; the
#: drainer adds ``correlation_id``, ``causation_id``, ``entity_id``, and
#: ``session_id``, and ``hook_fired_at`` (the fire instant; the event's own
#: ``emitted_at`` is authoritative); governed redaction adds
#: ``redaction_state``. Each duplicates or describes what the event states in
#: ``lineage``. In particular, ``lineage.turn_id`` is authoritative and the
#: appender's top-level ``turn_id`` is dropped by name -- never by a blanket
#: "ignore extras", which would also admit a content-bearing key.
TRANSPORT_STAMP_KEYS: frozenset[str] = frozenset(
    {
        "causation_id",
        "correlation_id",
        "entity_id",
        "hook_fired_at",
        "lane_source",
        "lane_ticket",
        "redaction_state",
        "session_id",
        "turn_id",
        "workspace_path",
    }
)


def _canonical_goal_id(payload: dict[str, JsonValue], key: str) -> str | None:
    """Return the goal id under ``key``, or refuse a value that is not canonical.

    OMN-20031. Absent and null are both accepted. A present value must be the
    canonical lowercase hyphenated UUID text, which is what the producer stamps
    (``str(UUID(...))``): anything else is refused so it dead-letters rather
    than being coerced or stored.
    """
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"payload.{key} must be a uuid string or null")
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise ValueError(f"payload.{key} is not a uuid") from None
    if canonical != value:
        raise ValueError(f"payload.{key} is not a canonical lowercase uuid")
    return value


def _goal_uuid(value: JsonValue) -> UUID | None:
    return UUID(value) if isinstance(value, str) else None


class ModelClaudeHookContentRefWire(BaseModel):
    """One content field exactly as the harness sent it (OMN-19513).

    ``value`` is raw: the producer neither scrubs nor hashes it (operator
    RULING 2026-10-15T04:24:42Z on the rolling work ledger).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(min_length=1)
    value: str
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
    agent_id: str | None = Field(min_length=1)
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
    agent_model: str | None = None
    agent_description: str | None = Field(default=None, max_length=200)
    workflow_phase: str | None = None

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
    #: OMN-19513: the producer's hook_actor_envelope.yaml declares claude, codex, unknown.
    actor: Literal["claude", "codex", "unknown"]
    claude_code_version: str | None = None
    #: OMN-17427: the hostname of the machine that emitted the event.
    host: str | None = Field(default=None, min_length=1)
    #: OMN-17427: the appender's lane stamp. Empty means the emitter asked and
    #: resolved no lane; the fold stores that as NULL.
    lane: str | None = None
    #: OMN-18531: the capture hook's item-identity stamp on a lineage event. This
    #: topic carries the lineage stream only (the content stream travels on the
    #: restricted content family), so any other stream value is refused. The
    #: sequence is per session and per stream, starting at 1. The row retains
    #: it for the session barrier; its identity remains ``event_id``.
    stream: Literal["lineage"] | None = None
    producer_seq: int | None = Field(default=None, ge=1, strict=True)
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
        goal_id = _canonical_goal_id(self.payload, "goal_id")
        parent_goal_id = _canonical_goal_id(self.payload, "parent_goal_id")
        if parent_goal_id is not None and goal_id is None:
            raise ValueError("payload.parent_goal_id is set without payload.goal_id")
        exit_code = self.payload.get("exit_code")
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise ValueError("payload.exit_code must be an integer or null")
        self._validate_derived_payload()

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

    def _validate_derived_payload(self) -> None:
        """OMN-18946: the two derived events state their facts or are refused."""
        if self.hook_event_name is EnumClaudeHookEventName.GUARD_REFUSAL:
            for key in ("guard", "reason_class"):
                value = self.payload.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"GuardRefusal needs a non-blank payload.{key}")
        elif self.hook_event_name is EnumClaudeHookEventName.ASSISTANT_USAGE:
            model = self.payload.get("model")
            if not isinstance(model, str) or not model.strip():
                raise ValueError("AssistantUsage needs a non-blank payload.model")
            for key in _USAGE_COUNT_KEYS:
                value = self.payload.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(
                        f"AssistantUsage payload.{key} must be a non-negative integer"
                    )

    def _derived_text(self, event: EnumClaudeHookEventName, key: str) -> str | None:
        if self.hook_event_name is not event:
            return None
        value = self.payload.get(key)
        return value if isinstance(value, str) else None

    def _usage_count(self, key: str) -> int | None:
        if self.hook_event_name is not EnumClaudeHookEventName.ASSISTANT_USAGE:
            return None
        value = self.payload.get(key)
        return value if isinstance(value, int) else None

    @property
    def guard(self) -> str | None:
        """The guard that refused the call (``GuardRefusal`` only)."""
        return self._derived_text(EnumClaudeHookEventName.GUARD_REFUSAL, "guard")

    @property
    def refusal_reason(self) -> str | None:
        """The guard's normalised reason class (``GuardRefusal`` only)."""
        return self._derived_text(EnumClaudeHookEventName.GUARD_REFUSAL, "reason_class")

    @property
    def input_tokens(self) -> int | None:
        return self._usage_count("input_tokens")

    @property
    def output_tokens(self) -> int | None:
        return self._usage_count("output_tokens")

    @property
    def cache_creation_input_tokens(self) -> int | None:
        return self._usage_count("cache_creation_input_tokens")

    @property
    def cache_read_input_tokens(self) -> int | None:
        return self._usage_count("cache_read_input_tokens")

    @property
    def assistant_message_count(self) -> int | None:
        return self._usage_count("message_count")

    @property
    def goal_id(self) -> UUID | None:
        return _goal_uuid(self.payload.get("goal_id"))

    @property
    def parent_goal_id(self) -> UUID | None:
        return _goal_uuid(self.payload.get("parent_goal_id"))

    @property
    def exit_code(self) -> int | None:
        value = self.payload.get("exit_code")
        return value if isinstance(value, int) else None

    @property
    def model(self) -> str | None:
        """The model that ran the event, where the event states one.

        A subagent's model rides ``lineage.agent_model`` (the harness sidecar);
        the main thread's is stated only by ``SessionStart``'s ``model``.
        """
        if (
            self.hook_event_name is not EnumClaudeHookEventName.ASSISTANT_USAGE
            and self.lineage.agent_model
        ):
            # An AssistantUsage event states the API's model id itself, which is
            # more exact than the sidecar's alias (``sonnet``).
            return self.lineage.agent_model
        value = self.payload.get("model")
        return value if isinstance(value, str) and value else None

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
