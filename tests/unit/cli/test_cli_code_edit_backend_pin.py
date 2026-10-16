# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex code-edit run --backend-id`` pins every model turn to one host (OMN-20514).

The in-process delegate route spreads first-choice traffic across lab hosts, so
a caller that must keep a run off one host needs a pin it can state, a record of
the host before the first turn, and a refusal when a turn lands elsewhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner, Result
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.cli import cli_code_edit
from omnimarket.delegated_code_edit import loop_ports
from omnimarket.delegated_code_edit.loop_ports import (
    DelegatePinUnresolvedError,
    pin_violation,
    resolve_delegate_pin,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator import ModelDelegatePin

pytestmark = pytest.mark.unit

HOST_A = "lab-host-a"
HOST_B = "lab-host-b"
BACKENDS = {
    "local-coder": SimpleNamespace(
        endpoint_url=f"http://{HOST_A}:8000/v1/chat/completions",
        model_name="Qwen3.8-27B",
    ),
    "local-omnipc2-chat": SimpleNamespace(
        endpoint_url=f"http://{HOST_B}:8000/v1/chat/completions",
        model_name="Qwen3.8-27B",
    ),
    "parked": SimpleNamespace(endpoint_url=None, model_name=None),
}
PIN_A = ModelDelegatePin(
    backend_id="local-coder",
    host=HOST_A,
    endpoint_url=BACKENDS["local-coder"].endpoint_url,
    model_name="Qwen3.8-27B",
)
REPLY: dict[str, object] = {
    "actions": [
        {
            "tool": "edit",
            "file_path": "src/m.py",
            "old_string": "VALUE = 0",
            "new_string": "VALUE = 1",
        },
        {"tool": "finish", "summary": "VALUE is 1"},
    ]
}


@pytest.fixture(autouse=True)
def _backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop_ports, "resolve_bifrost_backend", BACKENDS.get)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "src").mkdir(parents=True)
    (root / "src" / "m.py").write_text("VALUE = 0\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


def _write_receipt(
    run_dir: Path,
    *,
    backend_id: str | None,
    attempt_hosts: tuple[str, ...],
    honoured: bool | None = None,
) -> None:
    attempts = [{"backend_id": backend_id, "host": host} for host in attempt_hosts]
    receipt: dict[str, object] = {
        "backend_id": backend_id,
        "backend_pin_honoured": honoured,
        "receipt": {
            "result": {"terminal_payload": {"payload": {"attempts": attempts}}}
        },
    }
    (run_dir / "receipt.json").write_text(json.dumps(receipt))


class _Delegate:
    """A scripted ``onex delegate``: records argv and what the loop had written."""

    def __init__(self, state: Path, *, backend_id: str | None, hosts: tuple[str, ...]):
        self.state = state
        self.backend_id = backend_id
        self.hosts = hosts
        self.argvs: list[list[str]] = []
        self.pre_run_seen: list[dict[str, object] | None] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.argvs.append(argv)
        pre_run = list((self.state / "runs").glob("*/pre_run.json"))
        self.pre_run_seen.append(
            json.loads(pre_run[0].read_text()) if pre_run else None
        )
        run_id = str(uuid.uuid4())
        run_dir = self.state / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "result.txt").write_text(json.dumps(REPLY))
        _write_receipt(run_dir, backend_id=self.backend_id, attempt_hosts=self.hosts)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"run_id": run_id}), "")


def _invoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delegate: _Delegate,
    *extra: str,
) -> tuple[Result, Path]:
    tree = _tree(tmp_path)
    task = tmp_path / "task.md"
    task.write_text("set VALUE to 1")
    monkeypatch.setattr(cli_code_edit, "delegate_runner", lambda: delegate)
    result = CliRunner().invoke(
        cli_code_edit.code_edit_group,
        [
            "run",
            "--worktree",
            str(tree),
            "--task-file",
            str(task),
            "--writable",
            "src/*.py",
            "--check",
            "value=grep -q 'VALUE = 1' src/m.py",
            "--state-root",
            str(delegate.state),
            "--onex",
            "/bin/onex",
            *extra,
        ],
    )
    return result, delegate.state


def test_pinned_run_sends_the_pin_and_records_the_host_before_the_first_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delegate = _Delegate(tmp_path / "state", backend_id="local-coder", hosts=(HOST_A,))
    result, state = _invoke(
        tmp_path, monkeypatch, delegate, "--backend-id", "local-coder"
    )
    assert result.exit_code == 0, result.output
    assert f"delegate pin: backend_id=local-coder host={HOST_A}" in result.output
    assert len(delegate.argvs) == 1
    argv = delegate.argvs[0]
    assert argv[argv.index("--backend-id") + 1] == "local-coder"
    # The record existed, naming the host, when the first model turn started.
    first = delegate.pre_run_seen[0]
    assert first is not None
    assert first["delegate_pin"] == PIN_A.model_dump(mode="json")
    line = json.loads(result.output.strip().splitlines()[-1])
    assert line["status"] == "accepted"
    receipt = json.loads(
        (state / "runs" / line["loop_run_id"] / "loop_receipt.json").read_text()
    )
    assert receipt["delegate_pin"]["host"] == HOST_A


def test_a_turn_served_by_the_other_host_ends_the_loop_with_no_further_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delegate = _Delegate(
        tmp_path / "state", backend_id="local-omnipc2-chat", hosts=(HOST_B,)
    )
    result, _ = _invoke(tmp_path, monkeypatch, delegate, "--backend-id", "local-coder")
    assert result.exit_code == 3, result.output
    line = json.loads(result.output.strip().splitlines()[-1])
    assert line["status"] == "delegate_failed"
    assert len(delegate.argvs) == 1
    assert "local-omnipc2-chat" in line["detail"]
    assert HOST_B in line["detail"]
    assert HOST_A in line["detail"]


def test_an_attempt_on_the_other_host_after_the_pinned_one_failed_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delegate = _Delegate(
        tmp_path / "state", backend_id="local-coder", hosts=(HOST_A, HOST_B)
    )
    result, _ = _invoke(tmp_path, monkeypatch, delegate, "--backend-id", "local-coder")
    line = json.loads(result.output.strip().splitlines()[-1])
    assert line["status"] == "delegate_failed"
    assert f"an attempt on host {HOST_B}" in line["detail"]


@pytest.mark.parametrize("backend_id", ["no-such-backend", "parked"])
def test_an_unresolvable_pin_is_refused_before_any_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_id: str
) -> None:
    delegate = _Delegate(tmp_path / "state", backend_id=None, hosts=())
    result, state = _invoke(tmp_path, monkeypatch, delegate, "--backend-id", backend_id)
    assert result.exit_code == 1, result.output
    assert "cannot pin turns" in result.output
    assert delegate.argvs == []
    assert not list(state.glob("runs/*/pre_run.json"))


def test_an_unpinned_run_sends_no_pin_and_records_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delegate = _Delegate(tmp_path / "state", backend_id=None, hosts=())
    result, _ = _invoke(tmp_path, monkeypatch, delegate)
    assert result.exit_code == 0, result.output
    assert "--backend-id" not in delegate.argvs[0]
    first = delegate.pre_run_seen[0]
    assert first is not None
    assert first["delegate_pin"] is None


def test_resolve_delegate_pin_reads_the_host_from_the_endpoint() -> None:
    assert resolve_delegate_pin("local-omnipc2-chat").host == HOST_B
    with pytest.raises(DelegatePinUnresolvedError):
        resolve_delegate_pin("parked")


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        ({"backend_id": "local-coder"}, ""),
        ({"backend_id": "local-coder", "backend_pin_honoured": True}, ""),
        (
            {"backend_id": "local-coder", "backend_pin_honoured": False},
            "not honoured",
        ),
        ({"backend_id": "local-omnipc2-chat"}, "backend 'local-omnipc2-chat'"),
        ({}, "names no backend"),
    ],
)
def test_pin_violation_reads_the_delegate_receipt(
    tmp_path: Path, receipt: dict[str, object], expected: str
) -> None:
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    found = pin_violation(path, PIN_A)
    assert (expected in found) if expected else (found == "")


def test_pin_violation_treats_a_missing_receipt_as_unverifiable(tmp_path: Path) -> None:
    assert "unreadable" in pin_violation(tmp_path / "absent.json", PIN_A)


def test_pin_violation_reads_attempts_where_the_in_process_receipt_keeps_them(
    tmp_path: Path,
) -> None:
    """Live in-process receipts hold ``attempts`` at ``receipt.result.attempts``."""
    path = tmp_path / "receipt.json"
    attempts = [{"backend_id": "local-coder", "host": HOST_B}]
    path.write_text(
        json.dumps(
            {
                "backend_id": "local-coder",
                "backend_pin_honoured": True,
                "receipt": {"result": {"attempts": attempts}},
            }
        )
    )
    assert f"an attempt on host {HOST_B}" in pin_violation(path, PIN_A)
