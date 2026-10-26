"""The idle-turn guard (OMN-17427): an orchestrator turn that read free lab headroom or an UNOWNED red from the
throughput tick or queue-status and dispatched nothing is refused at Stop.

Run: uv run pytest -q tests/nodes/node_idle_slot_fill_effect/test_idle_turn_guard.py

The guard runs the way the Stop hook calls it: a child process reading the Stop payload on stdin, with the
transcript a scratch file. The failing case is the 2026-10-18 shape (~19:45Z to 21:25Z): the tick printed
MISSING lab-headroom on every run and the turn flagged it and ended; the control is the same turn with a dispatch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

GUARD_MAIN = (
    "from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import guard_main; "
    "raise SystemExit(guard_main())"
)
TICK = (
    "MISSING lab-headroom:host-a | placed units 2 of cap 16, free Claude slots 14 | FIX: dispatch\n"
    "MISSING lab-headroom:host-d | placed units 1 of cap 16, free Claude slots 15 | FIX: dispatch\n"
    "THROUGHPUT STALL n=2 unknown=0: lab-headroom:host-a, lab-headroom:host-d"
)
TICK_CMD = 'python3 "$P/skills/session-start/scripts/throughput_tick.py"'


def transcript(tmp: Path, *turn: dict[str, Any]) -> Path:
    path = tmp / "t.jsonl"
    rows = [
        {"type": "user", "message": {"role": "user", "content": "run the tick"}},
        *turn,
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def call(uid: str, name: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": uid, "name": name, "input": data}],
        },
    }


def result(uid: str, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": uid,
                    "content": [{"type": "text", "text": text}],
                }
            ],
        },
    }


def say(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def run(path: Path, *, force: bool = True) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_CODE_ENTRYPOINT", "ONEX_IDLE_GUARD")
    }
    if force:
        env["ONEX_IDLE_GUARD"] = "force"
    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": str(path),
            "stop_hook_active": False,
        }
    )
    return subprocess.run(
        [sys.executable, "-B", "-c", GUARD_MAIN],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )


def test_a_turn_that_flags_free_slots_and_dispatches_nothing_is_refused(
    tmp_path: Path,
) -> None:
    path = transcript(
        tmp_path,
        call("a", "Bash", {"command": TICK_CMD}),
        result("a", TICK),
        say("29 free slots; the sweep planned 0. Flagging it."),
    )
    out = run(path)
    assert out.returncode == 0, out.stderr
    decision = json.loads(out.stdout)
    assert decision["decision"] == "block"
    assert "MISSING lab-headroom:host-a" in decision["reason"]
    assert "dispatched nothing" in decision["reason"]


def test_the_same_turn_with_a_dispatch_passes(tmp_path: Path) -> None:
    path = transcript(
        tmp_path,
        call("a", "Bash", {"command": TICK_CMD}),
        result("a", TICK),
        call(
            "b",
            "Bash",
            {
                "command": "python3 onex_remote_lane.py run --brief b.md --lane x --model opus"
            },
        ),
        result("b", "DETACHED lane=x"),
        say("sent one lane"),
    )
    out = run(path)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "", out.stdout


def test_an_earlier_turn_s_reading_does_not_count(tmp_path: Path) -> None:
    path = transcript(
        tmp_path,
        call("a", "Bash", {"command": TICK_CMD}),
        result("a", TICK),
        say("flagged"),
        {"type": "user", "message": {"role": "user", "content": "what is 2+2"}},
        say("4"),
    )
    assert run(path).stdout == ""


def test_a_headless_lane_is_not_checked(tmp_path: Path) -> None:
    path = transcript(
        tmp_path,
        call("a", "Bash", {"command": TICK_CMD}),
        result("a", TICK),
        say("flagged"),
    )
    assert run(path, force=False).stdout == ""


def test_an_unreadable_payload_refuses_loudly(tmp_path: Path) -> None:
    out = run(tmp_path / "absent.jsonl")
    assert out.returncode == 2
    assert "idle-guard" in out.stderr
