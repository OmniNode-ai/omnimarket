# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Engine-native receipt usage; recorded fixtures contain no prompts or tool output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_remote_lane_close_compute.handlers import (
    HandlerRemoteLaneResult,
)
from omnimarket.nodes.node_remote_lane_close_compute.models import (
    ModelRemoteLaneResultRequest,
)

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parent / "fixtures"


def close(
    engine: str = "claude", events: list[dict[str, Any]] | None = None, **kwargs: Any
) -> dict[str, Any]:
    result = HandlerRemoteLaneResult().handle(
        ModelRemoteLaneResultRequest(
            engine_exit_code=0,
            engine=engine,
            engine_output=events,
            final_message="LANE_RESULT outcome=done\nDELEGATION delegated=1 codex=fixture",
            **kwargs,
        )
    )
    return result.model_dump(mode="json")


def test_receipt_carries_claude_usage() -> None:
    usage = close(
        events=[
            {
                "type": "result",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 7,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 3,
                    "tool_calls": 4,
                },
                "num_turns": 2,
                "duration_ms": 1500,
                "total_cost_usd": 0.25,
                "modelUsage": {"claude-sonnet-4-6": {}},
            }
        ]
    )["usage"]
    assert usage == {
        "engine": "claude",
        "model": "claude-sonnet-4-6",
        "tokens_in": 12,
        "tokens_out": 7,
        "tokens_cached": 5,
        "cache_read": 5,
        "cache_write": 3,
        "turns": 2,
        "tool_calls": 4,
        "retries": "UNKNOWN",
        "wall_seconds": 1.5,
        "cost_basis": "MEASURED",
        "total_cost_usd": 0.25,
    }


def test_codex_cost_unknown_not_zero() -> None:
    usage = close(
        "codex",
        [
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 40,
                    "output_tokens": 9,
                    "cached_input_tokens": 13,
                    "tool_calls": 6,
                },
            }
        ],
    )["usage"]
    assert (
        usage["tokens_in"],
        usage["tokens_out"],
        usage["tokens_cached"],
        usage["tool_calls"],
    ) == (40, 9, 13, 6)
    assert usage["cost_basis"] == "UNKNOWN"
    assert "total_cost_usd" not in usage


@pytest.mark.parametrize("engine", ["claude", "codex", "glm"])
def test_missing_usage_unknown(engine: str) -> None:
    usage = close(engine)["usage"]
    assert all(
        usage[k] == "UNKNOWN"
        for k in (
            "tokens_in",
            "tokens_out",
            "tokens_cached",
            "cache_read",
            "cache_write",
            "turns",
            "tool_calls",
            "retries",
            "wall_seconds",
            "model",
        )
    )
    assert usage["cost_basis"] == "UNKNOWN"
    assert "total_cost_usd" not in usage


def test_contract_id_in_receipt() -> None:
    result = close(brief="Contract: contract-42\nAttempt: attempt-2\n")
    assert result["usage"]["contract_id"] == "contract-42"
    assert result["ledger_cells"] == {
        "contract_id": "contract-42",
        "attempt_id": "attempt-2",
    }
    without_identity = close()
    assert "contract_id" not in without_identity["usage"]
    assert without_identity["ledger_cells"] == {}


def test_attempt_id_in_receipt() -> None:
    result = close(brief="Attempt: attempt-2\n")
    without_identity = close()
    assert result["usage"]["attempt_id"] == "attempt-2"
    assert "attempt_id" not in without_identity["usage"]


@pytest.mark.parametrize(
    "brief",
    ["Contract: one\nContract: two", "Contract: one two", "Attempt: one\nAttempt: two"],
)
def test_ambiguous_brief_refused(brief: str) -> None:
    with pytest.raises(ValueError, match="ambiguous or invalid"):
        close(brief=brief)


def test_metadata_in_fences_is_not_a_contract() -> None:
    result = close(brief="```\nContract: example\nAttempt: example\n```")
    assert result["ledger_cells"] == {}


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_recorded_stream_fixtures(engine: str) -> None:
    # Recorded real tails: fixtures/claude_recorded_events.json and fixtures/codex_recorded_events.json.
    fixture = json.loads((FIXTURES / f"{engine}_recorded_events.json").read_text())
    usage = close(engine, fixture["events"])["usage"]
    assert usage["tokens_in"] > 0
    assert usage["tokens_out"] > 0
    assert usage["tool_calls"] > 0
    expected = json.loads((FIXTURES / "recorded_expected.json").read_text())[engine]
    assert usage == expected


def test_counter_semantics_recorded_streams() -> None:
    # Authored counter trace isolates assistant-message, tool-type and retry semantics.
    fixture = json.loads((FIXTURES / "counter_events.json").read_text())
    for engine, events in fixture.items():
        usage = close(engine, events)["usage"]
        assert (usage["turns"], usage["tool_calls"], usage["retries"]) == (3, 4, 1)


@pytest.mark.parametrize(
    ("engine", "counts"), [("claude", (67, 84, 3)), ("codex", (9, 166, 2))]
)
def test_counter_semantics_recorded_streams_real_retries(
    engine: str, counts: tuple[int, int, int]
) -> None:
    # Real failed/repeated calls: fixtures/claude_recorded_retries.json and fixtures/codex_recorded_retries.json.
    fixture = json.loads((FIXTURES / f"{engine}_recorded_retries.json").read_text())
    usage = close(engine, fixture["events"])["usage"]
    assert (usage["turns"], usage["tool_calls"], usage["retries"]) == counts


def test_codex_turn_usage_is_summed_without_double_counting_items() -> None:
    usage = close(
        "codex",
        [
            {
                "type": "item.started",
                "item": {"id": "c1", "type": "command_execution", "command": "false"},
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "c1",
                    "type": "command_execution",
                    "command": "false",
                    "exit_code": 1,
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cached_input_tokens": 3,
                },
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 20, "output_tokens": 4},
            },
        ],
    )["usage"]
    assert (usage["tokens_in"], usage["tokens_out"], usage["tokens_cached"]) == (
        30,
        6,
        "UNKNOWN",
    )
    assert usage["tool_calls"] == 1


def test_claude_message_fragments_do_not_double_count_turns_or_calls() -> None:
    event = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {"command": "true"},
                }
            ],
        },
    }
    usage = close(events=[event, event, {"type": "result", "usage": {}}])["usage"]
    assert usage["turns"] == 1
    assert usage["tool_calls"] == 1


def test_invalid_numeric_usage_is_unknown() -> None:
    usage = close(
        events=[
            {
                "type": "result",
                "usage": {
                    "input_tokens": True,
                    "output_tokens": -1,
                    "cache_read_input_tokens": "12",
                },
                "total_cost_usd": float("nan"),
            }
        ]
    )["usage"]
    assert (
        usage["tokens_in"] == usage["tokens_out"] == usage["tokens_cached"] == "UNKNOWN"
    )
    assert usage["cost_basis"] == "UNKNOWN"


def test_explicit_zero_is_measured() -> None:
    usage = close(
        events=[
            {
                "type": "result",
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "total_cost_usd": 0,
            }
        ]
    )["usage"]
    assert usage["tokens_in"] == usage["tokens_out"] == 0
    assert usage["cost_basis"] == "MEASURED"
    assert usage["total_cost_usd"] == 0


def test_usage_does_not_change_the_closing_decision() -> None:
    result = close(events=[{"type": "result", "usage": {}}])
    assert result["outcome"] == "done"
    assert result["problem"] is None
