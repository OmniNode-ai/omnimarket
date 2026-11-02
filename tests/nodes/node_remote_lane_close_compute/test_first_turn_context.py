# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""First-turn context by source on the lane receipt (OMN-21889)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_remote_lane_close_compute.handlers import (
    HandlerRemoteLaneResult,
)
from omnimarket.nodes.node_remote_lane_close_compute.handlers.first_turn_context import (
    COLUMNS,
    first_turn_context_table,
)
from omnimarket.nodes.node_remote_lane_close_compute.models import (
    ModelRemoteLaneResultRequest,
)

pytestmark = pytest.mark.unit

INIT = {
    "type": "system",
    "subtype": "init",
    "tools": ["Bash", "Read", "Edit"],
    "plugins": [{"name": "omni"}, {"name": "onex"}],
    "skills": ["omni:ci-watch", "onex:preflight"],
}


def assistant(msg_id: str, inp: int, read: int, write: int) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "usage": {
                "input_tokens": inp,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write,
            },
        },
    }


def receipt(events: list[dict[str, Any]] | None, brief: str = "") -> dict[str, Any]:
    result = HandlerRemoteLaneResult().handle(
        ModelRemoteLaneResultRequest(
            engine_exit_code=0,
            engine="claude",
            engine_output=events,
            brief=brief,
            final_message="LANE_RESULT outcome=done\nDELEGATION delegated=1 codex=x",
        )
    )
    return result.model_dump(mode="json")["first_turn_context"]


def test_first_turn_context_receipt() -> None:
    context = receipt(
        [
            INIT,
            assistant("m1", 2, 12676, 43650),
            assistant("m1", 2, 12676, 43650),
            assistant("m2", 3, 56326, 900),
        ],
        brief="héllo",
    )
    assert context == {
        "first_turn_input_tokens": 2,
        "first_turn_cache_read": 12676,
        "first_turn_cache_write": 43650,
        "first_turn_context_tokens": 56328,
        "brief_bytes": 6,
        "skill_listing_count": 2,
        "skill_listing_bytes": len(b"omni:ci-watch\nonex:preflight"),
        "tools_count": 3,
        "plugins_count": 2,
        "total_turns": 2,
        "peak_context_tokens": 57229,
    }


def test_first_turn_context_unknown() -> None:
    context = receipt(None)
    assert set(context.values()) == {"UNKNOWN"}
    assert 0 not in context.values()
    no_usage = receipt([{"type": "assistant", "message": {"id": "m1"}}])
    assert no_usage["first_turn_input_tokens"] == "UNKNOWN"
    assert no_usage["peak_context_tokens"] == "UNKNOWN"
    assert no_usage["total_turns"] == 1
    partial = receipt(
        [{"type": "assistant", "message": {"id": "m", "usage": {"input_tokens": 5}}}]
    )
    assert partial["first_turn_input_tokens"] == 5
    assert partial["first_turn_context_tokens"] == "UNKNOWN"


def test_first_turn_context_table() -> None:
    log = "wrapper line\n" + "\n".join(
        json.dumps(e) for e in [INIT, assistant("m1", 2, 10, 20)]
    )
    table = first_turn_context_table(
        [("lane-a", log, "brief"), ("lane-b", "no json here", None)]
    ).splitlines()
    assert table[0] == "| lane | " + " | ".join(COLUMNS) + " |"
    assert len(table) == 4
    assert table[2].startswith("| lane-a | 2 | 10 | 20 | 32 | 5 |")
    assert table[3] == "| lane-b | " + " | ".join(["UNKNOWN"] * len(COLUMNS)) + " |"
