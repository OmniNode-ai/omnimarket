# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The idle-turn guard's decision as a node (OMN-19998).

Run: uv run pytest -q tests/nodes/node_idle_turn_guard_compute/test_idle_turn_guard_decision.py

The recorded case, 2026-11-04: the guard blocked 36 Stop turns, 33 of them that day. Five came after the operator
wrote stop. Each one listed hosts that refuse lanes as free headroom. It also blocked a turn whose real send had
started in the background, because the send was not a tool call it could see. Three rules answer those cases: a
hold or stop row in force holds the guard; a host that refuses lanes is not headroom; a lane the runner placed, or
is placing, inside the turn's window counts as a dispatch. The guard reads runner receipts for that.

Each case below runs twice: through the node's handler, and through the Stop hook the way queue_status.py
--idle-guard calls it (a child process reading the Stop payload, with the adapter's facts in it).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.idle_turn_guard import (
    EnumIdleTurnGuardVerdict,
    ModelIdleTurnGuardDecisionRequest,
    ModelIdleTurnGuardFacts,
)
from omnimarket.nodes.node_idle_turn_guard_compute import NodeIdleTurnGuardCompute

START = datetime(2026, 11, 4, 21, 0, tzinfo=UTC)
NOW = START + timedelta(minutes=4)
# The tick's lines on the recorded turns: one host that takes lanes, two that refuse them.
TICK = (
    "MISSING lab-headroom:host-a | placed units 2 of cap 16, free Claude slots 14 | FIX: dispatch\n"
    "MISSING lab-headroom:host-r | placed units 0 of cap 16, free Claude slots 16 | FIX: dispatch\n"
    "MISSING lab-headroom:host-m | placed units 0 of cap 8, free Claude slots 8 | FIX: dispatch"
)
REFUSING = ("host-r", "host-m")
STOP_ROW = {
    "id": "hold-stop-2104",
    "kind": "stop",
    "stamp": "2026-11-04T21:01:00Z",
    "scope": "dispatch",
    "text": "operator: stop",
}


def request(
    headroom: list[str], facts: dict[str, Any], *, dispatched: bool = False
) -> ModelIdleTurnGuardDecisionRequest:
    return ModelIdleTurnGuardDecisionRequest(
        headroom_lines=tuple(headroom),
        tool_dispatched=dispatched,
        facts=ModelIdleTurnGuardFacts.model_validate(facts),
        now=NOW,
    )


def facts(**extra: Any) -> dict[str, Any]:
    return {"window_start": START.isoformat(), **extra}


def decide(headroom: list[str], data: dict[str, Any], **kw: Any) -> Any:
    return NodeIdleTurnGuardCompute().handle(request(headroom, data, **kw))


# ---- the pure decision ----------------------------------------------------------------------------------------------


def test_free_headroom_and_no_dispatch_refuses() -> None:
    out = decide(TICK.splitlines(), facts())
    assert out.verdict is EnumIdleTurnGuardVerdict.REFUSE
    assert len(out.shown) == 3


def test_a_stop_row_in_force_holds_the_guard() -> None:
    out = decide(TICK.splitlines(), facts(holds_in_force=[STOP_ROW]))
    assert out.verdict is EnumIdleTurnGuardVerdict.HELD
    assert "hold-stop-2104" in out.reason


def test_a_hold_row_in_force_holds_the_guard() -> None:
    hold = {**STOP_ROW, "id": "hold-dispatch-1", "kind": "hold"}
    out = decide(TICK.splitlines(), facts(holds_in_force=[hold]))
    assert out.verdict is EnumIdleTurnGuardVerdict.HELD


def test_a_host_that_refuses_lanes_is_not_headroom() -> None:
    out = decide(TICK.splitlines(), facts(lane_refusing_hosts=list(REFUSING)))
    assert out.verdict is EnumIdleTurnGuardVerdict.REFUSE
    assert [line.split(" ")[1] for line in out.shown] == ["lab-headroom:host-a"]
    assert sorted(out.discounted_hosts) == sorted(REFUSING)


def test_only_refusing_hosts_free_passes() -> None:
    lines = [line for line in TICK.splitlines() if "host-a" not in line]
    out = decide(lines, facts(lane_refusing_hosts=list(REFUSING)))
    assert out.verdict is EnumIdleTurnGuardVerdict.PASS


@pytest.mark.parametrize("state", ["placed", "placing"])
def test_a_lane_placed_or_placing_in_the_window_is_a_dispatch(state: str) -> None:
    receipt = {
        "lane": "fill-1",
        "state": state,
        "at": (START + timedelta(minutes=2)).isoformat(),
    }
    out = decide(TICK.splitlines(), facts(runner_receipts=[receipt]))
    assert out.verdict is EnumIdleTurnGuardVerdict.PASS
    assert out.receipt_lanes == ("fill-1",)


def test_a_receipt_from_before_the_window_is_not_a_dispatch() -> None:
    receipt = {
        "lane": "old-1",
        "state": "placed",
        "at": (START - timedelta(minutes=30)).isoformat(),
    }
    out = decide(TICK.splitlines(), facts(runner_receipts=[receipt]))
    assert out.verdict is EnumIdleTurnGuardVerdict.REFUSE


def test_a_refused_receipt_is_not_a_dispatch() -> None:
    receipt = {
        "lane": "r-1",
        "state": "refused",
        "at": (START + timedelta(minutes=1)).isoformat(),
    }
    out = decide(TICK.splitlines(), facts(runner_receipts=[receipt]))
    assert out.verdict is EnumIdleTurnGuardVerdict.REFUSE


def test_a_tool_dispatch_still_passes() -> None:
    out = decide(TICK.splitlines(), facts(), dispatched=True)
    assert out.verdict is EnumIdleTurnGuardVerdict.PASS


def test_same_snapshot_same_decision() -> None:
    data = facts(lane_refusing_hosts=["host-r"])
    assert decide(TICK.splitlines(), data) == decide(TICK.splitlines(), data)


# ---- through the Stop hook, as the adapter calls it ------------------------------------------------------------------

GUARD_MAIN = (
    "from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import guard_main; "
    "raise SystemExit(guard_main())"
)
TICK_CMD = 'python3 "$P/skills/session-start/scripts/queue_status.py"'


def _transcript(tmp: Path) -> Path:
    rows: list[dict[str, Any]] = [
        {
            "type": "user",
            "timestamp": START.isoformat(),
            "message": {"role": "user", "content": "run the tick"},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "a",
                        "name": "Bash",
                        "input": {"command": TICK_CMD},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "a",
                        "content": [{"type": "text", "text": TICK}],
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "flagged"}],
            },
        },
    ]
    path = tmp / "t.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _hook(
    path: Path, guard_facts: dict[str, Any] | None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_ENTRYPOINT"}
    env["ONEX_IDLE_GUARD"] = "force"
    payload: dict[str, Any] = {
        "hook_event_name": "Stop",
        "transcript_path": str(path),
        "stop_hook_active": False,
    }
    if guard_facts is not None:
        payload["idle_guard_facts"] = guard_facts
    return subprocess.run(
        [sys.executable, "-B", "-c", GUARD_MAIN],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )


def test_hook_without_facts_still_refuses(tmp_path: Path) -> None:
    out = _hook(_transcript(tmp_path), None)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["decision"] == "block"


def test_hook_after_the_operator_wrote_stop_passes(tmp_path: Path) -> None:
    out = _hook(_transcript(tmp_path), {"holds_in_force": [STOP_ROW]})
    assert out.returncode == 0, out.stderr
    assert out.stdout == ""


def test_hook_with_only_refusing_hosts_named_still_refuses_for_the_one_free_host(
    tmp_path: Path,
) -> None:
    out = _hook(_transcript(tmp_path), {"lane_refusing_hosts": list(REFUSING)})
    assert out.returncode == 0, out.stderr
    reason = json.loads(out.stdout)["reason"]
    assert "host-a" in reason
    assert "host-r" not in reason
    assert "host-m" not in reason


def test_hook_with_a_background_send_in_the_window_passes(tmp_path: Path) -> None:
    receipt = {
        "lane": "bg-1",
        "state": "placing",
        "at": (START + timedelta(minutes=1)).isoformat(),
    }
    out = _hook(_transcript(tmp_path), {"runner_receipts": [receipt]})
    assert out.returncode == 0, out.stderr
    assert out.stdout == ""


def test_hook_window_defaults_to_the_operator_prompt_stamp(tmp_path: Path) -> None:
    old = {
        "lane": "old-1",
        "state": "placed",
        "at": (START - timedelta(hours=1)).isoformat(),
    }
    out = _hook(_transcript(tmp_path), {"runner_receipts": [old]})
    assert json.loads(out.stdout)["decision"] == "block"


def test_hook_with_malformed_facts_refuses_loudly(tmp_path: Path) -> None:
    out = _hook(_transcript(tmp_path), {"runner_receipts": [{"lane": "x"}]})
    assert out.returncode == 2
    assert "idle-guard" in out.stderr
