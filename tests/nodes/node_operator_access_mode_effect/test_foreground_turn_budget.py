# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The main session's tool time in one turn is counted, not only each call (OMN-20893).

Operator 2026-11-25 11:58Z: a chain of Bash calls of about 100 seconds each passed the per-call
ceiling (120 s) and held the foreground for minutes. Past the declared turn budget the next call
that is not a short read is refused and names the detached form.
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

from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_foreground_bash_guard import (
    HandlerForegroundBashGuard,
)
from omnimarket.nodes.node_operator_access_mode_effect.models.model_foreground_bash_request import (
    DEFAULT_TURN_BUDGET_SECONDS,
    ModelForegroundBashRequest,
)
from omnimarket.nodes.node_operator_access_mode_effect.runtime_operator_hook_cli import (
    turn_tool_seconds,
)

ENTRY = "omnimarket.nodes.node_operator_access_mode_effect.runtime_operator_hook_cli"
T0 = datetime(2026, 11, 25, 11, 50, 0, tzinfo=UTC)


def _stamp(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _transcript(path: Path, calls: list[float], *, prompt_text: str = "go") -> Path:
    """One operator prompt, then one Bash call per entry that ran that many seconds."""
    rows: list[dict[str, Any]] = [
        {
            "type": "user",
            "timestamp": _stamp(0),
            "message": {"role": "user", "content": prompt_text},
        }
    ]
    clock = 1.0
    for i, took in enumerate(calls):
        tid = f"toolu_{i}"
        rows.append(
            {
                "type": "assistant",
                "timestamp": _stamp(clock),
                "message": {
                    "content": [
                        {"type": "tool_use", "id": tid, "name": "Bash", "input": {}}
                    ]
                },
            }
        )
        clock += took
        rows.append(
            {
                "type": "user",
                "timestamp": _stamp(clock),
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": tid, "content": "ok"}
                    ]
                },
            }
        )
        clock += 1.0
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _ask(command: str, seconds: float, **extra: Any) -> bool:
    return (
        HandlerForegroundBashGuard()
        .handle(
            ModelForegroundBashRequest(
                command, True, True, turn_tool_seconds=seconds, **extra
            )
        )
        .allowed
    )


def test_the_declared_budget_is_sixty_seconds_and_matches_the_contract() -> None:
    assert DEFAULT_TURN_BUDGET_SECONDS == 60
    contract = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_operator_access_mode_effect/contract.yaml"
    ).read_text(encoding="utf-8")
    assert "foreground_turn_budget_seconds: 60" in contract


def test_the_recorded_chain_of_hundred_second_calls_is_refused_past_the_budget(
    tmp_path: Path,
) -> None:
    transcript = _transcript(tmp_path / "t.jsonl", [100.0, 100.0, 100.0])
    assert turn_tool_seconds({"transcript_path": str(transcript)}) == pytest.approx(
        300.0
    )
    verdict = HandlerForegroundBashGuard().handle(
        ModelForegroundBashRequest(
            "python3 /opt/probe.py", True, True, turn_tool_seconds=300.0
        )
    )
    assert not verdict.allowed
    assert "nohup <command> > <file> 2>&1 &" in verdict.reason
    assert "background Workflow" in verdict.reason
    assert "OMN-20893" in verdict.reason


def test_a_known_good_short_turn_passes(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path / "t.jsonl", [5.0, 20.0])
    seconds = turn_tool_seconds({"transcript_path": str(transcript)})
    assert seconds == pytest.approx(25.0)
    assert _ask("python3 /opt/probe.py", seconds)


@pytest.mark.parametrize(
    "command",
    [
        "git -C /repo status",
        "git -C /repo log --oneline -3",
        "ls /repo",
        "cat /repo/README.md",
        "grep -n foo /repo/a.py",
        "tail -n 20 /var/log/x.log",
        "nohup python3 /opt/probe.py > /tmp/p.log 2>&1 &",
    ],
)
def test_short_reads_and_the_detached_form_pass_past_the_budget(command: str) -> None:
    assert _ask(command, 300.0)


@pytest.mark.parametrize(
    "command",
    [
        "python3 /opt/probe.py",
        "uv run pytest tests/",
        "git -C /repo push",
        "make build",
    ],
)
def test_anything_else_is_refused_past_the_budget(command: str) -> None:
    assert not _ask(command, 300.0)


def test_a_slow_call_dressed_as_a_read_is_refused() -> None:
    assert not _ask("cat /repo/a.py", 300.0, timeout_ms=100_000)


def test_the_budget_is_not_reached_at_exactly_sixty() -> None:
    assert _ask("python3 /opt/probe.py", 60.0)
    assert not _ask("python3 /opt/probe.py", 60.5)


def test_a_lane_and_a_headless_run_are_not_counted() -> None:
    for main, facing in ((False, True), (True, False)):
        request = ModelForegroundBashRequest(
            "python3 /opt/probe.py", main, facing, turn_tool_seconds=300.0
        )
        assert HandlerForegroundBashGuard().handle(request).allowed


def test_a_new_operator_prompt_starts_a_new_turn(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path / "t.jsonl", [100.0, 100.0])
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": _stamp(500),
                    "message": {"content": [{"type": "text", "text": "next"}]},
                }
            )
            + "\n"
        )
    assert turn_tool_seconds({"transcript_path": str(transcript)}) == 0.0


def test_a_stop_hook_reprompt_does_not_start_a_new_turn(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path / "t.jsonl", [100.0, 100.0])
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": _stamp(400),
                    "message": {
                        "content": "Stop hook feedback:\nBLOCKED (operator reply)"
                    },
                }
            )
            + "\n"
        )
    assert turn_tool_seconds({"transcript_path": str(transcript)}) == pytest.approx(
        200.0
    )


def test_an_unreadable_transcript_counts_nothing(tmp_path: Path) -> None:
    assert turn_tool_seconds({}) == 0.0
    assert turn_tool_seconds({"transcript_path": str(tmp_path / "none")}) == 0.0


def _hook(
    payload: dict[str, Any], budget: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent"}
    env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
    env["OMNI_ACCESS_MODE_STATE_DIR"] = payload["state"]
    if budget is not None:
        env["ONEX_FOREGROUND_TURN_BUDGET_SECONDS"] = budget
    body = {k: v for k, v in payload.items() if k != "state"}
    return subprocess.run(
        [sys.executable, "-B", "-m", ENTRY, "pre-tool"],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )


def test_the_hook_entry_counts_the_turn_and_never_blocks_answering(
    tmp_path: Path,
) -> None:
    transcript = _transcript(tmp_path / "t.jsonl", [100.0, 100.0, 100.0])
    base = {
        "session_id": "s1",
        "transcript_path": str(transcript),
        "state": str(tmp_path),
    }
    refused = _hook(
        {**base, "tool_name": "Bash", "tool_input": {"command": "python3 /opt/p.py"}}
    )
    assert refused.returncode == 2, refused.stderr
    assert "nohup <command> > <file> 2>&1 &" in refused.stderr
    read = _hook({**base, "tool_name": "Bash", "tool_input": {"command": "ls /repo"}})
    assert read.returncode == 0, read.stderr
    # answering the operator is never a Bash call and is never counted
    reply = _hook(
        {**base, "tool_name": "SendUserMessage", "tool_input": {"message": "Done."}}
    )
    assert reply.returncode == 0, reply.stderr
    lane = _hook(
        {
            **base,
            "agent_id": "a1",
            "tool_name": "Bash",
            "tool_input": {"command": "python3 /opt/p.py"},
        }
    )
    assert lane.returncode == 0, lane.stderr
    # the declared budget can be raised by the environment
    raised = _hook(
        {**base, "tool_name": "Bash", "tool_input": {"command": "python3 /opt/p.py"}},
        budget="900",
    )
    assert raised.returncode == 0, raised.stderr
