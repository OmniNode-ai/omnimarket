# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19458 AC1 -- ``onex test-loop run`` runs the loop and prints its terminal.

The command runs on the in-memory bus: the loop publishes each focused run as
a command, the in-process host serves it, and the loop reads the receipt back
from the terminal. The model and the lab host are the two seams replaced here
(``delegate_runner`` and ``lab_handler``); every compute, the orchestrator, the
bus client and the host are the real ones.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.cli import cli_test_loop
from omnimarket.nodes.node_push_validation_effect import (
    EnumFocusedTestRunStatus,
    ModelFocusedTestRunReceipt,
    ModelFocusedTestRunRequest,
)

pytestmark = pytest.mark.unit

_JUNIT = Path(__file__).parents[2] / "nodes/node_pytest_failure_digest_compute/fixtures"
_TARGET = "src/omnimarket/widget.py"
_TEST_PATH = "tests/unit/test_widget_dtl.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def _source_clone(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "omnimarket"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    target = repo / _TARGET
    target.parent.mkdir(parents=True)
    target.write_text("def widget() -> int:\n    return 0\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "prefix")
    prefix = _git(repo, "rev-parse", "HEAD")
    target.write_text("def widget() -> int:\n    return 1\n")
    _git(repo, "commit", "-q", "-am", "fixed")
    fixed = _git(repo, "rev-parse", "HEAD")
    return repo, fixed, prefix


class _LabHandler:
    """The lab host: the written test passes at the fixed ref and fails in the
    call phase at the pre-fix ref."""

    def __init__(self) -> None:
        self.roles: list[str] = []

    async def handle(
        self, request: ModelFocusedTestRunRequest
    ) -> ModelFocusedTestRunReceipt:
        self.roles.append(request.ref_role)
        passed = request.ref_role == "fixed"
        name = "junit_container_passed.xml" if passed else "junit_call.xml"
        return ModelFocusedTestRunReceipt(
            correlation_id=request.correlation_id,
            ref_role=request.ref_role,
            attempt=request.attempt,
            commit_sha=request.commit_sha,
            test_node_id=request.test_node_id,
            status=EnumFocusedTestRunStatus.COMPLETED,
            exit_code=0 if passed else 1,
            junit_xml=(_JUNIT / name).read_text(),
            host="local:lab",
            teardown_container_absent=True,
            teardown_worktree_absent=True,
        )


def _fake_delegate(
    state_root: Path, calls: list[list[str]]
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        run_id = str(uuid.uuid4())
        run_dir = state_root / "runs" / run_id
        run_dir.mkdir(parents=True)
        reply = {
            "test_path": _TEST_PATH,
            "test_source": (
                "from omnimarket.widget import widget\n\n\n"
                "def test_widget() -> None:\n    assert widget() == 1\n"
            ),
        }
        (run_dir / "result.txt").write_text("```json\n" + json.dumps(reply) + "\n```")
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"run_id": run_id}) + "\n", stderr=""
        )

    return run


def _write_request(tmp_path: Path, fixed: str, prefix: str) -> Path:
    request = {
        "correlation_id": str(uuid.uuid4()),
        "repo": "OmniNode-ai/omnimarket",
        "fixed_ref": fixed,
        "prefix_ref": prefix,
        "criterion": "AC1: widget() returns 1.",
        "target_path": _TARGET,
        "test_path": _TEST_PATH,
        "run_code_gates": False,
    }
    path = tmp_path / "loop.json"
    path.write_text(json.dumps(request))
    return path


def test_the_command_runs_the_loop_over_the_bus_and_prints_its_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, fixed, prefix = _source_clone(tmp_path)
    state_root = tmp_path / "state"
    calls: list[list[str]] = []
    lab = _LabHandler()
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setattr(cli_test_loop, "lab_handler", lambda _owners: lab)
    monkeypatch.setattr(
        cli_test_loop, "delegate_runner", lambda: _fake_delegate(state_root, calls)
    )
    request_path = _write_request(tmp_path, fixed, prefix)

    result = CliRunner().invoke(
        cli_test_loop.test_loop_group,
        [
            "run",
            "--request",
            str(request_path),
            "--bus",
            "inmemory",
            "--delegate-in-process",
            "--state-root",
            str(state_root),
            "--source-clone",
            str(clone),
        ],
    )

    assert result.exit_code == 0, result.output
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1
    terminal = json.loads(lines[0])
    assert terminal["status"] == "accepted_call"
    assert terminal["headline"] is True
    assert lab.roles == ["fixed", "prefix"]
    assert len(calls) == 1
    assert "--bus" in calls[0]
    assert calls[0][calls[0].index("--bus") + 1] == "inmemory"
    receipt = state_root / "runs" / terminal["loop_run_id"] / "loop_receipt.json"
    assert receipt.is_file()


def test_a_loop_that_is_not_accepted_prints_its_terminal_and_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, fixed, prefix = _source_clone(tmp_path)
    state_root = tmp_path / "state"

    class _NeverFails(_LabHandler):
        async def handle(
            self, request: ModelFocusedTestRunRequest
        ) -> ModelFocusedTestRunReceipt:
            receipt = await super().handle(
                request.model_copy(update={"ref_role": "fixed"})
            )
            return receipt.model_copy(update={"ref_role": request.ref_role})

    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setattr(cli_test_loop, "lab_handler", lambda _owners: _NeverFails())
    monkeypatch.setattr(
        cli_test_loop, "delegate_runner", lambda: _fake_delegate(state_root, [])
    )

    result = CliRunner().invoke(
        cli_test_loop.test_loop_group,
        [
            "run",
            "--request",
            str(_write_request(tmp_path, fixed, prefix)),
            "--bus",
            "inmemory",
            "--delegate-in-process",
            "--state-root",
            str(state_root),
            "--source-clone",
            str(clone),
        ],
    )

    assert result.exit_code == cli_test_loop.EXIT_NOT_ACCEPTED, result.output
    assert json.loads(result.stdout.strip())["status"] == "control_did_not_fail"


def test_the_deployed_lane_delegate_flags_are_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, fixed, prefix = _source_clone(tmp_path)
    state_root = tmp_path / "state"
    calls: list[list[str]] = []
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setattr(cli_test_loop, "lab_handler", lambda _owners: _LabHandler())
    monkeypatch.setattr(
        cli_test_loop, "delegate_runner", lambda: _fake_delegate(state_root, calls)
    )

    result = CliRunner().invoke(
        cli_test_loop.test_loop_group,
        [
            "run",
            "--request",
            str(_write_request(tmp_path, fixed, prefix)),
            "--bus",
            "inmemory",
            "--state-root",
            str(state_root),
            "--source-clone",
            str(clone),
        ],
    )

    assert result.exit_code == 0, result.output
    argv = calls[0]
    assert argv[argv.index("--bus") + 1] == "kafka"
    assert argv[argv.index("--lane") + 1] == "dev"
    assert argv[argv.index("--locus") + 1] == "deployed-lane"


def test_a_kafka_run_that_names_no_broker_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, fixed, prefix = _source_clone(tmp_path)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    result = CliRunner().invoke(
        cli_test_loop.test_loop_group,
        [
            "run",
            "--request",
            str(_write_request(tmp_path, fixed, prefix)),
            "--state-root",
            str(tmp_path / "state"),
            "--source-clone",
            str(clone),
        ],
    )

    assert result.exit_code == 1
    assert "run bus" in result.output


class _SuiteHost:
    """Runs each file once: test_a passes, test_b fails in the call phase; the
    first request is refused busy once."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, int]] = []

    async def handle(
        self, request: ModelFocusedTestRunRequest
    ) -> ModelFocusedTestRunReceipt:
        self.seen.append((request.test_node_id, request.attempt))
        common = {
            "correlation_id": request.correlation_id,
            "ref_role": request.ref_role,
            "attempt": request.attempt,
            "commit_sha": request.commit_sha,
            "test_node_id": request.test_node_id,
            "host": "local:lab",
        }
        if len(self.seen) == 1:
            return ModelFocusedTestRunReceipt(
                status=EnumFocusedTestRunStatus.HOST_BUSY, **common
            )
        passed = request.test_node_id.endswith("test_a.py")
        name = "junit_container_passed.xml" if passed else "junit_call.xml"
        return ModelFocusedTestRunReceipt(
            status=EnumFocusedTestRunStatus.COMPLETED,
            exit_code=0 if passed else 1,
            junit_xml=(_JUNIT / name).read_text(),
            teardown_container_absent=True,
            teardown_worktree_absent=True,
            **common,
        )


def test_run_focused_runs_each_file_over_the_bus_and_sums_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _SuiteHost()
    sleeps: list[float] = []
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setattr(cli_test_loop, "lab_handler", lambda _owners: host)
    monkeypatch.setattr(cli_test_loop, "busy_sleep", sleeps.append)

    result = CliRunner().invoke(
        cli_test_loop.test_loop_group,
        [
            "run-focused",
            "--repo",
            "OmniNode-ai/omnimarket",
            "--commit",
            "8e5b1dbeffb80b6afbe603e80f7c9fe281647fa5",
            "--node-id",
            "tests/a/test_a.py",
            "--node-id",
            "tests/b/test_b.py",
            "--bus",
            "inmemory",
            "--state-root",
            str(tmp_path / "state"),
        ],
    )

    assert result.exit_code == cli_test_loop.EXIT_NOT_ACCEPTED, result.output
    line = json.loads(result.stdout.strip())
    assert line["summary"] == "1 failed, 1 passed"
    assert line["exit"] == 1
    assert [r["status"] for r in line["runs"]] == ["completed", "completed"]
    assert host.seen == [
        ("tests/a/test_a.py", 1),
        ("tests/a/test_a.py", 1),
        ("tests/b/test_b.py", 2),
    ]
    assert sleeps == [cli_test_loop.HOST_BUSY_BACKOFF_SECONDS]
    receipts = list((tmp_path / "state" / "runs").glob("*/focused/*.json"))
    assert len(receipts) == 2


def test_summary_line_uses_pytest_vocabulary() -> None:
    assert cli_test_loop.summary_line(64, 0, 0, 0) == "64 passed"
    assert (
        cli_test_loop.summary_line(3, 1, 1, 2)
        == "1 failed, 3 passed, 2 skipped, 1 error"
    )
    assert cli_test_loop.summary_line(0, 0, 0, 0) == "no tests ran"
