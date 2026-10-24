# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18946: a refused call names its guard, and an event carries token use.

Two events the omniclaude plugin derives, because no hook states them:

* ``GuardRefusal`` -- a guard refused a call. 3,939 refused calls in a week had
  no guard name: the guard lived only in rate-limited ledger FRICTION rows that
  join to no event. The row carries ``guard`` and ``refusal_reason`` and the
  refused call's ``tool_use_id``, so it joins the call's ``PreToolUse``.
* ``AssistantUsage`` -- tokens and model per assistant message, read from the
  transcript. No event carried tokens, and the model rode 37.5% of events.

The fixtures under ``tests/fixtures/claude_hook_capture/events`` are copied from
omniclaude's generator (``scripts/generate_claude_hook_capture_fixtures.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_claude_hook_events.handlers import (
    handler_claude_hook_events_writer as writer_module,
)
from omnimarket.nodes.node_projection_claude_hook_events.handlers.handler_projection_claude_hook_events import (
    HandlerProjectionClaudeHookEvents,
)
from omnimarket.nodes.node_projection_claude_hook_events.models import (
    EnumClaudeHookEventName,
    ModelClaudeHookEventWire,
    ModelClaudeHookProjectionRequest,
)
from omnimarket.nodes.node_projection_claude_hook_events.models.enum_claude_hook_event_name import (
    TOOL_CALL_COMPLETIONS,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_hook_capture"
MIGRATIONS = Path(writer_module.__file__).resolve().parent.parent / "migrations"


def _event(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURES / "events" / f"{name}.json").read_text(encoding="utf-8")
    )
    return loaded


def _fold(event: dict[str, Any]) -> Any:
    return (
        HandlerProjectionClaudeHookEvents()
        .handle(ModelClaudeHookProjectionRequest.model_validate(event))
        .event_row
    )


def test_the_enum_carries_the_two_derived_events() -> None:
    assert EnumClaudeHookEventName.GUARD_REFUSAL.value == "GuardRefusal"
    assert EnumClaudeHookEventName.ASSISTANT_USAGE.value == "AssistantUsage"


# --------------------------------------------------------------------------
# GuardRefusal
# --------------------------------------------------------------------------


def test_a_refusal_folds_to_a_row_naming_the_guard_and_the_reason() -> None:
    row = _fold(_event("GuardRefusal"))

    assert row.hook_event_name is EnumClaudeHookEventName.GUARD_REFUSAL
    assert row.guard == "pre_tool_use_worktree_guard.sh"
    assert row.refusal_reason == "worktree-add-refused"
    assert row.tool_use_id == "toolu-fixture-refused"
    assert row.tool_name == "Bash"


def test_a_refusal_joins_the_refused_call_by_causation_and_tool_use_id() -> None:
    event = _event("GuardRefusal")
    row = _fold(event)

    assert str(row.causation_id) == event["payload"]["tool_call_key"]
    assert row.causation_id is not None


def test_a_refusal_row_states_no_token_use() -> None:
    row = _fold(_event("GuardRefusal"))

    assert row.input_tokens is None
    assert row.output_tokens is None
    assert row.assistant_message_count is None


def test_a_refusal_with_no_guard_is_refused_not_stored() -> None:
    event = _event("GuardRefusal")
    del event["payload"]["guard"]

    with pytest.raises(ValidationError):
        ModelClaudeHookEventWire.model_validate(event)


def test_a_refusal_with_a_blank_reason_is_refused_not_stored() -> None:
    event = _event("GuardRefusal")
    event["payload"]["reason_class"] = ""

    with pytest.raises(ValidationError):
        ModelClaudeHookEventWire.model_validate(event)


def test_a_refusal_carrying_a_content_key_is_refused() -> None:
    event = _event("GuardRefusal")
    event["payload"]["detail"] = "BLOCKED: git worktree add /tmp/x"

    with pytest.raises(ValidationError):
        ModelClaudeHookEventWire.model_validate(event)


# --------------------------------------------------------------------------
# AssistantUsage
# --------------------------------------------------------------------------


def test_a_usage_event_folds_to_a_row_with_tokens_and_the_api_model() -> None:
    row = _fold(_event("AssistantUsage"))

    assert row.hook_event_name is EnumClaudeHookEventName.ASSISTANT_USAGE
    assert row.model == "model-fixture-1"
    assert row.input_tokens == 12
    assert row.output_tokens == 345
    assert row.cache_creation_input_tokens == 6
    assert row.cache_read_input_tokens == 78
    assert row.assistant_message_count == 2
    assert row.guard is None
    assert row.refusal_reason is None


def test_the_model_an_event_ran_is_the_api_model_not_the_sidecar_alias() -> None:
    """A subagent's sidecar says ``sonnet``; the API message says which model ran."""
    event = _event("AssistantUsage")
    event["lineage"]["agent_id"] = "agent-1"
    event["lineage"]["is_subagent"] = True
    event["lineage"]["agent_model"] = "sonnet"

    assert _fold(event).model == "model-fixture-1"


def test_an_ordinary_subagent_event_still_takes_the_sidecar_model() -> None:
    event = _event("PostToolUse")
    event["lineage"]["agent_id"] = "agent-1"
    event["lineage"]["is_subagent"] = True
    event["lineage"]["agent_model"] = "sonnet"

    assert _fold(event).model == "sonnet"


@pytest.mark.parametrize(
    "key",
    [
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "message_count",
    ],
)
def test_a_negative_or_non_integer_count_is_refused(key: str) -> None:
    for bad in (-1, "12", 1.5, True):
        event = _event("AssistantUsage")
        event["payload"][key] = bad
        with pytest.raises(ValidationError):
            ModelClaudeHookEventWire.model_validate(event)


def test_a_usage_event_with_no_model_is_refused() -> None:
    event = _event("AssistantUsage")
    del event["payload"]["model"]

    with pytest.raises(ValidationError):
        ModelClaudeHookEventWire.model_validate(event)


def test_every_other_hook_leaves_the_new_columns_null() -> None:
    for name in ("PreToolUse", "PostToolUse", "Stop", "SessionStart"):
        row = _fold(_event(name))
        assert row.guard is None, name
        assert row.refusal_reason is None, name
        assert row.input_tokens is None, name
        assert row.output_tokens is None, name
        assert row.cache_creation_input_tokens is None, name
        assert row.cache_read_input_tokens is None, name
        assert row.assistant_message_count is None, name


# --------------------------------------------------------------------------
# neither is a tool call
# --------------------------------------------------------------------------


def test_a_derived_event_inside_a_subagent_adds_no_tool_call() -> None:
    assert EnumClaudeHookEventName.GUARD_REFUSAL not in TOOL_CALL_COMPLETIONS
    assert EnumClaudeHookEventName.ASSISTANT_USAGE not in TOOL_CALL_COMPLETIONS
    assert "GuardRefusal" not in writer_module._UPSERT_SPAN
    assert "AssistantUsage" not in writer_module._UPSERT_SPAN


# --------------------------------------------------------------------------
# the writer and the migration
# --------------------------------------------------------------------------

_NEW_COLUMNS = (
    "guard",
    "refusal_reason",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "assistant_message_count",
)


def test_the_insert_writes_every_new_column() -> None:
    statement = writer_module._INSERT_EVENT
    for column in _NEW_COLUMNS:
        assert column in statement, column


def test_a_new_migration_adds_every_column_and_asserts_its_shape() -> None:
    migration = (MIGRATIONS / "0007_add_guard_refusal_and_token_usage.sql").read_text(
        encoding="utf-8"
    )
    for column in _NEW_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column} " in migration, column
        assert f"AND column_name = '{column}'" in migration, column
    # Token counts outgrow INTEGER on a long session summed in SQL; the cells
    # themselves are BIGINT so a SUM never overflows a column type.
    for column in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        assert f"{column} BIGINT" in migration, column
    assert "DROP " not in migration.upper().replace("DROP-", "")
    assert "DO $$" not in migration


def test_the_row_params_follow_the_column_order() -> None:
    row = _fold(_event("AssistantUsage"))
    params = writer_module._event_params(row)

    assert params[-8:-1] == (None, None, 12, 345, 6, 78, 2)
    assert params[-1] is None  # OMN-21334: an unstamped event has no sequence.
