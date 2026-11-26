# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A round never starts while the previous run of the same round is alive, and every run moves its heartbeat.

``claude`` is a stand-in that records its arguments. The lock holder is a separate live process, as a
previous run of the round would be.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_session_round_effect.handlers import (
    handler_session_round as handler,
)
from omnimarket.nodes.node_session_round_effect.handlers.handler_session_round import (
    EXIT_BUSY,
    HandlerSessionRound,
)
from omnimarket.nodes.node_session_round_effect.models.model_session_round import (
    ModelSessionRoundRequest,
)

pytestmark = pytest.mark.unit

ROUND = "round-fill"


def _fake_claude(tmp_path: Path, *, sleep: float = 0.0, exit_code: int = 0) -> Path:
    fake = tmp_path / "fake-claude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys, time\n"
        "calls = pathlib.Path(sys.argv[0]).with_name('claude-calls.jsonl')\n"
        "with calls.open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print('round output', flush=True)\n"
        f"time.sleep({sleep})\n"
        f"sys.exit({exit_code})\n"
    )
    fake.chmod(0o755)
    return fake


def _claude_calls(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / "claude-calls.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def _holder(lock_file: Path) -> subprocess.Popen[str]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "os.write(fd, f'{os.getpid()}\\n'.encode())\n"
            "print('held', flush=True)\n"
            "time.sleep(120)\n",
            str(lock_file),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    return holder


def _request(state: Path, claude: Path, timeout: int = 60) -> ModelSessionRoundRequest:
    return ModelSessionRoundRequest(
        round_name=ROUND,
        state_dir=state,
        prompt="run the fill round",
        claude=str(claude),
        timeout_seconds=timeout,
        claude_arguments=("--model", "opus"),
    )


def test_a_live_holder_makes_a_second_start_exit_without_running_the_round(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    request = _request(state, _fake_claude(tmp_path))
    holder = _holder(state / f"{ROUND}.lock")
    try:
        busy = HandlerSessionRound().handle(request)
    finally:
        holder.kill()
        holder.wait()
    assert busy.outcome == "busy"
    assert busy.exit_code == EXIT_BUSY
    assert busy.holder_pid == str(holder.pid)
    assert _claude_calls(tmp_path) == []  # the round did not run
    assert not (
        state / f"{ROUND}.heartbeat.json"
    ).exists()  # a refusal moves no heartbeat
    assert json.loads((state / f"{ROUND}.skip.json").read_text())["holder_pid"] == str(
        holder.pid
    )
    # The holder is dead, so the kernel dropped its lock: the next start runs the round.
    ran = HandlerSessionRound().handle(request)
    assert ran.outcome == "finished"
    assert len(_claude_calls(tmp_path)) == 1


def test_with_no_holder_the_round_runs_and_writes_its_heartbeat(tmp_path: Path) -> None:
    state = tmp_path / "state"
    request = _request(state, _fake_claude(tmp_path, exit_code=3))
    result = HandlerSessionRound().handle(request)
    assert (result.outcome, result.exit_code) == ("finished", 3)
    assert _claude_calls(tmp_path) == [["-p", "run the fill round", "--model", "opus"]]
    beat = json.loads(result.heartbeat_file.read_text())
    assert beat["state"] == "finished"
    assert beat["exit_code"] == 3
    assert beat["started_at"] <= beat["finished_at"]
    assert (state / f"{ROUND}.last.out").read_text() == "round output\n"
    # The lock is released when a run ends, so back-to-back starts never contend.
    assert HandlerSessionRound().handle(request).outcome == "finished"


def test_a_round_past_its_limit_is_ended_and_marked_timed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler, "KILL_GRACE_SECONDS", 1)
    request = _request(tmp_path / "state", _fake_claude(tmp_path, sleep=30), timeout=1)
    result = HandlerSessionRound().handle(request)
    assert result.outcome == "timed-out"
    assert result.exit_code != 0
    assert json.loads(result.heartbeat_file.read_text())["state"] == "timed-out"


def test_a_missing_claude_is_not_started_and_still_moves_the_heartbeat(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path / "state", tmp_path / "no-such-claude")
    result = HandlerSessionRound().handle(request)
    assert (result.outcome, result.exit_code) == ("not-started", 127)
    assert json.loads(result.heartbeat_file.read_text())["state"] == "not-started"


def test_the_agent_command_exits_75_under_a_live_holder_and_runs_without_one(
    tmp_path: Path,
) -> None:
    """The command the launchd agent runs, in a separate process, with the unit's environment."""
    state = tmp_path / "state"
    claude = _fake_claude(tmp_path)
    command = [
        sys.executable,
        "-m",
        "omnimarket.nodes.node_session_round_effect",
        "--omni-home",
        str(tmp_path),
        "--state-dir",
        str(state),
    ]
    env = {
        "PATH": "/usr/bin:/bin",
        "ONEX_ROUND": ROUND,
        "ONEX_ROUND_PROMPT": "run the fill round",
        "ONEX_ROUND_CLAUDE": str(claude),
        "ONEX_ROUND_TIMEOUT_S": "60",
        "ONEX_ROUND_CLAUDE_ARGS": "--model opus",
    }
    holder = _holder(state / f"{ROUND}.lock")
    try:
        busy = subprocess.run(
            command, capture_output=True, text=True, env=env, check=False
        )
    finally:
        holder.kill()
        holder.wait()
    assert busy.returncode == EXIT_BUSY, busy.stderr
    assert "BUSY" in busy.stderr
    assert _claude_calls(tmp_path) == []
    free = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    assert free.returncode == 0, free.stderr
    assert _claude_calls(tmp_path) == [["-p", "run the fill round", "--model", "opus"]]
    assert (state / f"{ROUND}.heartbeat.json").is_file()


def test_the_probe_checks_inputs_and_runs_nothing(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "omnimarket.nodes.node_session_round_effect",
        "--state-dir",
        str(tmp_path / "state"),
        "--probe",
    ]
    env = {"PATH": "/usr/bin:/bin", "ONEX_ROUND": ROUND, "ONEX_ROUND_PROMPT": "p"}
    missing = subprocess.run(
        command, capture_output=True, text=True, env=env, check=False
    )
    assert missing.returncode == 2  # no timeout declared
    env["ONEX_ROUND_TIMEOUT_S"] = "60"
    ok = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    assert ok.returncode == 0, ok.stderr
    assert not (tmp_path / "state" / f"{ROUND}.lock").exists()
