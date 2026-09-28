# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionClaudeHookEvents -- the pure fold over one hook event.

OMN-19513, build task 3 of the all-hooks capture split. The definition-B half
of the rule-7a projection pair: typed in, typed out, no clock, no broker, no
database. ``ClaudeHookEventsProjectionWriter`` beside it is the effect half the
runtime calls; it resolves the one fact this fold cannot know (which agent made
an earlier tool call) and hands it in.

What the fold decides, per the capture contract's ``projection.fold_rules``:

* ``parent_agent_id`` -- the ``agent_id`` of the ``PreToolUse`` in the same
  session whose ``tool_use_id`` equals this event's ``parent_tool_use_id``.
* ``parent_resolution`` -- ``main_thread`` when that ``PreToolUse`` was found
  and carried no ``agent_id``; ``resolved`` when it carried one; ``workflow``
  when the sidecar was a Workflow sidecar (``workflow_run_id`` set, no spawning
  call); ``unknown`` otherwise. ``unknown`` is never ``main_thread``.
* the span contribution of every event fired inside a subagent, and
* for a ``PreToolUse``, the children it resolves if they were written first.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.contract_topics import contract_subscribe_topics
from omnimarket.nodes.node_projection_claude_hook_events.models import (
    EnumClaudeHookEventName,
    EnumParentResolution,
    ModelChildParentRepair,
    ModelClaudeAgentSpanUpdate,
    ModelClaudeHookEventRow,
    ModelClaudeHookEventWire,
    ModelClaudeHookProjectionRequest,
    ModelClaudeHookProjectionResult,
    ModelKnownParentToolCall,
)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def _load_source_topic() -> str:
    """The one subscribe topic, read from the contract, never from a literal."""
    topics = contract_subscribe_topics(_CONTRACT_PATH)
    if len(topics) != 1:
        raise ValueError(
            "node_projection_claude_hook_events must subscribe exactly one topic "
            f"(the all-hook-types capture topic); found {len(topics)}."
        )
    return topics[0]


SOURCE_TOPIC = _load_source_topic()


def resolution_of(call: ModelKnownParentToolCall) -> EnumParentResolution:
    """A found spawning call is ``main_thread`` when no agent made it. Pure."""
    if call.agent_id is None:
        return EnumParentResolution.MAIN_THREAD
    return EnumParentResolution.RESOLVED


def resolve_parent(
    event: ModelClaudeHookEventWire,
    known_parent_calls: tuple[ModelKnownParentToolCall, ...],
) -> tuple[str | None, EnumParentResolution]:
    """Apply the contract's parent rule to one event. Pure."""
    lineage = event.lineage
    if lineage.parent_tool_use_id is not None:
        for call in known_parent_calls:
            if (
                call.session_id == lineage.session_id
                and call.tool_use_id == lineage.parent_tool_use_id
            ):
                return call.agent_id, resolution_of(call)
        return None, EnumParentResolution.UNKNOWN
    if lineage.workflow_run_id is not None:
        return None, EnumParentResolution.WORKFLOW
    return None, EnumParentResolution.UNKNOWN


class HandlerProjectionClaudeHookEvents:
    """Pure def-B reducer: one hook event in, its rows out."""

    def handle(
        self, request: ModelClaudeHookProjectionRequest
    ) -> ModelClaudeHookProjectionResult:
        event = request.event
        lineage = event.lineage
        parent_agent_id, parent_resolution = resolve_parent(
            event, request.known_parent_calls
        )

        event_row = ModelClaudeHookEventRow(
            event_id=event.event_id,
            session_id=lineage.session_id,
            agent_id=lineage.agent_id,
            is_subagent=lineage.is_subagent,
            agent_type=lineage.agent_type,
            parent_tool_use_id=lineage.parent_tool_use_id,
            parent_agent_id=parent_agent_id,
            workflow_run_id=lineage.workflow_run_id,
            spawn_depth=lineage.spawn_depth,
            hook_event_name=event.hook_event_name,
            tool_use_id=lineage.tool_use_id,
            tool_name=event.tool_name,
            prompt_id=lineage.prompt_id,
            turn_id=lineage.turn_id,
            correlation_id=lineage.correlation_id,
            causation_id=lineage.causation_id,
            emitted_at=event.emitted_at,
            payload=dict(event.payload),
            content_ref_ids=tuple(
                str(ref.content_record_id) for ref in event.content_refs
            ),
            source_topic=request.source_topic or SOURCE_TOPIC,
        )

        span_update: ModelClaudeAgentSpanUpdate | None = None
        if lineage.agent_id is not None:
            span_update = ModelClaudeAgentSpanUpdate(
                session_id=lineage.session_id,
                agent_id=lineage.agent_id,
                agent_type=lineage.agent_type,
                parent_tool_use_id=lineage.parent_tool_use_id,
                parent_agent_id=parent_agent_id,
                parent_resolution=parent_resolution,
                workflow_run_id=lineage.workflow_run_id,
                spawn_depth=lineage.spawn_depth,
                seen_at=event.emitted_at,
                stopped_at=(
                    event.emitted_at
                    if event.hook_event_name is EnumClaudeHookEventName.SUBAGENT_STOP
                    else None
                ),
            )

        resolves_children: ModelChildParentRepair | None = None
        if (
            event.hook_event_name is EnumClaudeHookEventName.PRE_TOOL_USE
            and lineage.tool_use_id is not None
        ):
            this_call = ModelKnownParentToolCall(
                session_id=lineage.session_id,
                tool_use_id=lineage.tool_use_id,
                agent_id=lineage.agent_id,
            )
            resolves_children = ModelChildParentRepair(
                session_id=lineage.session_id,
                parent_tool_use_id=lineage.tool_use_id,
                parent_agent_id=lineage.agent_id,
                parent_resolution=resolution_of(this_call),
            )

        return ModelClaudeHookProjectionResult(
            event_row=event_row,
            span_update=span_update,
            resolves_children=resolves_children,
        )


__all__ = [
    "SOURCE_TOPIC",
    "HandlerProjectionClaudeHookEvents",
    "resolution_of",
    "resolve_parent",
]
