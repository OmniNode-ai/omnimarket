# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex trajectory-eval``: the hand publisher of the trajectory evaluation commands.

Every publish goes to a capturing in-memory bus, so each test reads the exact
bytes the command would carry to the broker.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_trajectory_evaluation_effect import cli as cli_module
from omnimarket.nodes.node_trajectory_evaluation_effect.cli import (
    EXIT_RUN_FAILED,
    TrajectoryEvalUsageError,
    command_topics,
    resolve_repository,
    trajectory_eval_group,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    ModelTrajectoryEvaluationPoll,
    ModelTrajectoryEvaluationSubmit,
)

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "trajectory_eval"
    / "delegate_run_dir"
)
_RUN_CORRELATION_ID = "6f1c2a9e-3b4d-4e5f-8a7b-0c1d2e3f4a5b"


class _CapturingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> None:
        self.published.append((topic, key, value))


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> _CapturingBus:
    captured = _CapturingBus()

    def opener(
        *_: object,
    ) -> Callable[[], contextlib.AbstractAsyncContextManager[_CapturingBus]]:
        @contextlib.asynccontextmanager
        async def _open() -> AsyncIterator[_CapturingBus]:
            yield captured

        return _open

    monkeypatch.setattr(cli_module, "lab_run_bus_opener", opener)
    return captured


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _clone(tmp_path: Path, url: str) -> Path:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", url)
    return repo


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(trajectory_eval_group, list(args), catch_exceptions=False)


def _payload(value: bytes) -> dict[str, Any]:
    envelope = json.loads(value)
    payload = envelope["payload"]
    assert isinstance(payload, dict)
    return payload


# AC1 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/OmniNode-ai/omnimarket.git",
        "https://github.com/OmniNode-ai/omnimarket",
    ],
)
def test_repository_https_origin(tmp_path: Path, url: str) -> None:
    assert resolve_repository(_clone(tmp_path, url)) == "OmniNode-ai/omnimarket"


def test_repository_ssh_origin(tmp_path: Path) -> None:
    repo = _clone(tmp_path, "git@github.com:OmniNode-ai/omnimarket.git")
    assert resolve_repository(repo) == "OmniNode-ai/omnimarket"


def test_repository_non_github_remote_is_a_usage_error(tmp_path: Path) -> None:
    repo = _clone(tmp_path, "https://gitlab.example.com/owner/name.git")
    with pytest.raises(TrajectoryEvalUsageError):
        resolve_repository(repo)


def test_repository_outside_git_is_a_usage_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(TrajectoryEvalUsageError):
        resolve_repository(plain)


# AC2 -------------------------------------------------------------------------


def test_submit_one_command_per_run(tmp_path: Path, bus: _CapturingBus) -> None:
    repo = _clone(tmp_path, "git@github.com:OmniNode-ai/omnimarket.git")
    result = _invoke(
        "submit",
        "--run-dir",
        str(_FIXTURE),
        "--mode",
        "meta",
        "--repo-dir",
        str(repo),
        "--bus",
        "inmemory",
    )

    assert result.exit_code == 0, result.output
    assert len(bus.published) == 1
    topic, key, value = bus.published[0]
    assert topic == command_topics().submit
    assert key == _RUN_CORRELATION_ID.encode()
    payload = _payload(value)
    command = ModelTrajectoryEvaluationSubmit.model_validate(payload)
    assert str(command.correlation_id) == _RUN_CORRELATION_ID
    assert command.work_unit_repository == "OmniNode-ai/omnimarket"
    assert command.model_id == "synthetic-coder-27b"
    assert command.task_type == "code_generation"
    assert command.prompt_tokens == 412
    assert command.completion_tokens == 64
    assert command.latency_ms == 1830


def test_submit_dry_run_publishes_nothing(tmp_path: Path, bus: _CapturingBus) -> None:
    repo = _clone(tmp_path, "https://github.com/OmniNode-ai/omnimarket.git")
    result = _invoke(
        "submit",
        "--run-dir",
        str(_FIXTURE),
        "--mode",
        "meta",
        "--repo-dir",
        str(repo),
        "--dry-run",
    )

    assert result.exit_code == 0, result.output
    assert bus.published == []
    printed = json.loads(result.output.strip())
    assert printed["payload"]["correlation_id"] == _RUN_CORRELATION_ID


# AC3 -------------------------------------------------------------------------


def test_failed_run_publishes_nothing(tmp_path: Path, bus: _CapturingBus) -> None:
    run_dir = tmp_path / "run"
    shutil.copytree(_FIXTURE, run_dir)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    receipt["status"] = "failed"
    receipt["route_attributed"] = False
    (run_dir / "receipt.json").write_text(json.dumps(receipt))
    repo = _clone(tmp_path, "https://github.com/OmniNode-ai/omnimarket.git")

    result = _invoke(
        "submit",
        "--run-dir",
        str(run_dir),
        "--mode",
        "meta",
        "--repo-dir",
        str(repo),
        "--bus",
        "inmemory",
    )

    assert result.exit_code == EXIT_RUN_FAILED
    assert bus.published == []


def test_failed_run_missing_receipt_publishes_nothing(
    tmp_path: Path, bus: _CapturingBus
) -> None:
    run_dir = tmp_path / "run"
    shutil.copytree(_FIXTURE, run_dir)
    (run_dir / "receipt.json").unlink()
    repo = _clone(tmp_path, "https://github.com/OmniNode-ai/omnimarket.git")

    result = _invoke(
        "submit", "--run-dir", str(run_dir), "--mode", "meta", "--repo-dir", str(repo)
    )

    assert result.exit_code == EXIT_RUN_FAILED
    assert bus.published == []


# AC4 -------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["meta", "full"])
def test_mode_controls_the_texts(tmp_path: Path, bus: _CapturingBus, mode: str) -> None:
    repo = _clone(tmp_path, "https://github.com/OmniNode-ai/omnimarket.git")
    result = _invoke(
        "submit",
        "--run-dir",
        str(_FIXTURE),
        "--mode",
        mode,
        "--repo-dir",
        str(repo),
        "--bus",
        "inmemory",
    )

    assert result.exit_code == 0, result.output
    payload = _payload(bus.published[0][2])
    assert payload["content_mode"] == mode
    if mode == "meta":
        assert "prompt_text" not in payload
        assert "response_text" not in payload
    else:
        assert payload["prompt_text"] == (
            "Synthetic prompt: sort the list in place and return it."
        )
        assert payload["response_text"] == (
            "Synthetic answer: the list is sorted in place and returned.\n"
        )


# AC5 -------------------------------------------------------------------------

_PENDING_A = "rcpt-a"
_PENDING_B = "rcpt-b"
_DONE = "rcpt-done"


def _outcomes(tmp_path: Path) -> Path:
    rows: list[dict[str, Any]] = [
        {
            "correlation_id": "11111111-1111-4111-8111-111111111111",
            "content_mode": "meta",
            "idempotency_key": "k1",
            "backend_receipt_id": _PENDING_A,
            "duplicate": False,
            "verdict_state": "pending",
        },
        {
            "correlation_id": "22222222-2222-4222-8222-222222222222",
            "content_mode": "full",
            "idempotency_key": "k2",
            "backend_receipt_id": _DONE,
            "duplicate": False,
            "verdict_state": "pending",
        },
        # A later status read made this receipt terminal.
        {
            "payload": {
                "correlation_id": "22222222-2222-4222-8222-222222222222",
                "backend_receipt_id": _DONE,
                "verdict_state": "completed",
                "verdict": "pass",
            }
        },
        {
            "correlation_id": "33333333-3333-4333-8333-333333333333",
            "content_mode": "full",
            "idempotency_key": "k3",
            "backend_receipt_id": _PENDING_B,
            "duplicate": False,
            "verdict_state": "pending",
        },
        {
            "correlation_id": "44444444-4444-4444-8444-444444444444",
            "reason": "repository_not_public",
            "detail": "",
        },
    ]
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def test_poll_one_command_per_pending_receipt(
    tmp_path: Path, bus: _CapturingBus
) -> None:
    result = _invoke(
        "poll", "--outcomes", str(_outcomes(tmp_path)), "--bus", "inmemory"
    )

    assert result.exit_code == 0, result.output
    assert {topic for topic, _, _ in bus.published} == {command_topics().poll}
    commands = [
        ModelTrajectoryEvaluationPoll.model_validate(_payload(value))
        for _, _, value in bus.published
    ]
    assert [c.backend_receipt_id for c in commands] == [_PENDING_A, _PENDING_B]
    assert [c.content_mode for c in commands] == ["meta", "full"]


def test_poll_max_caps_the_commands(tmp_path: Path, bus: _CapturingBus) -> None:
    result = _invoke(
        "poll",
        "--outcomes",
        str(_outcomes(tmp_path)),
        "--max",
        "1",
        "--bus",
        "inmemory",
    )

    assert result.exit_code == 0, result.output
    assert len(bus.published) == 1
    assert _payload(bus.published[0][2])["backend_receipt_id"] == _PENDING_A


# AC6 -------------------------------------------------------------------------


def test_from_json_publishes_the_file_byte_for_byte(
    tmp_path: Path, bus: _CapturingBus
) -> None:
    raw = (
        b'{"payload": {"correlation_id": "55555555-5555-4555-8555-555555555555",'
        b' "work_unit_repository": "OmniNode-ai/omnimarket", "content_mode": "meta"},'
        b' "correlation_id": "55555555-5555-4555-8555-555555555555"}'
    )
    path = tmp_path / "command.json"
    path.write_bytes(raw)

    result = _invoke("submit", "--from-json", str(path), "--bus", "inmemory")

    assert result.exit_code == 0, result.output
    assert bus.published == [
        (
            command_topics().submit,
            b"55555555-5555-4555-8555-555555555555",
            raw,
        )
    ]


def test_from_json_refuses_a_file_that_is_not_a_submit_command(
    tmp_path: Path, bus: _CapturingBus
) -> None:
    path = tmp_path / "command.json"
    path.write_text('{"payload": {"work_unit_repository": "a/b"}}')

    result = _invoke("submit", "--from-json", str(path), "--bus", "inmemory")

    assert result.exit_code == 2
    assert bus.published == []


def test_submit_needs_exactly_one_source(bus: _CapturingBus) -> None:
    result = _invoke("submit", "--mode", "meta")
    assert result.exit_code == 2
    assert bus.published == []


# AC7 -------------------------------------------------------------------------


def test_entry_point_loads_the_group() -> None:
    found = [
        ep for ep in entry_points(group="onex.cli") if ep.name == "trajectory-eval"
    ]

    assert len(found) == 1
    assert found[0].dist is not None
    assert found[0].dist.name == "omnimarket"
    loaded: Callable[..., Any] = found[0].load()
    assert isinstance(loaded, click.Group)
    assert loaded is trajectory_eval_group
    assert set(loaded.commands) == {"submit", "poll"}


def test_entry_point_is_not_hand_wired() -> None:
    src = Path(cli_module.__file__).resolve().parents[2]
    offenders = [
        path
        for path in src.rglob("*.py")
        if "add_command(trajectory_eval_group" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_topics_come_from_the_contract() -> None:
    topics = command_topics()
    assert topics.submit == "onex.cmd.omnimarket.trajectory-evaluation-submit.v1"
    assert topics.poll == "onex.cmd.omnimarket.trajectory-evaluation-poll.v1"
