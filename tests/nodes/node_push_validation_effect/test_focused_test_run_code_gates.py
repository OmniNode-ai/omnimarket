# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19527 — the focused run also runs the repository's lint and type gates.

When a request names ``gate_paths``, the same throwaway container that runs
the focused test then runs ``ruff check``, ``ruff format --check`` and ``mypy
--strict`` over each named file, inside the task worktree (so the repository's
own configuration applies), and the receipt carries each gate's exit code and
output. The container's rails do not change: the validator still passes the
argument list, and a request without ``gate_paths`` builds the same list as
before.
"""

from __future__ import annotations

import shlex
from typing import Any

import pytest

from omnimarket.nodes.node_push_validation_effect.handlers.handler_focused_test_run_effect import (
    HandlerFocusedTestRunEffect,
)
from omnimarket.nodes.node_push_validation_effect.models.model_focused_test_run_receipt import (
    EnumFocusedTestRunStatus,
)
from omnimarket.nodes.node_push_validation_effect.models.model_focused_test_run_request import (
    ModelFocusedTestRunRequest,
)
from omnimarket.nodes.node_push_validation_effect.protocols.dtl_container_invocation import (
    CODE_GATE_COMMANDS,
    DtlInvocationRefusedError,
    ModelContainerRunSpec,
    build_docker_run_argv,
    gate_output_file,
    validate_docker_run_argv,
)
from tests.nodes.node_push_validation_effect.test_handler_focused_test_run_effect import (
    CORRELATION,
    NODE,
    ROOT,
    FakeClient,
    _request,
)

pytestmark = pytest.mark.unit

TEST_FILE = NODE.split("::", 1)[0]


def _spec(**overrides: Any) -> ModelContainerRunSpec:
    values: dict[str, Any] = {
        "task_root": ROOT,
        "task_dir": f"{ROOT}/tasks/{CORRELATION}/fixed-a1",
        "correlation_id": CORRELATION,
        "attempt": 1,
        "ref_role": "fixed",
        "image": "dtl-env:omnimarket-9317e28ad468-0f7b315b1ba6",
        "test_node_id": NODE,
        "timeout_seconds": 120,
    }
    values.update(overrides)
    return ModelContainerRunSpec(**values)


def test_the_three_gates_are_ruff_check_ruff_format_and_mypy_strict() -> None:
    names = [gate for gate, _ in CODE_GATE_COMMANDS]
    assert names == ["ruff_check", "ruff_format", "mypy"]
    argv = dict(CODE_GATE_COMMANDS)
    assert argv["ruff_check"][:3] == ("-m", "ruff", "check")
    assert argv["ruff_format"][:4] == ("-m", "ruff", "format", "--check")
    assert argv["mypy"][:3] == ("-m", "mypy", "--strict")


def test_without_gate_paths_the_container_command_is_unchanged() -> None:
    argv = build_docker_run_argv(_spec())
    image_at = argv.index("dtl-env:omnimarket-9317e28ad468-0f7b315b1ba6")
    assert argv[image_at + 1 : image_at + 4] == ["timeout", "--kill-after=10", "120"]
    assert argv[-1] == NODE
    assert "/bin/sh" not in argv


def test_with_gate_paths_pytest_runs_first_then_every_gate_on_every_path() -> None:
    argv = build_docker_run_argv(_spec(gate_paths=(TEST_FILE, "src/pkg/mod.py")))
    validate_docker_run_argv(argv, ROOT)  # the rails still hold
    image_at = argv.index("dtl-env:omnimarket-9317e28ad468-0f7b315b1ba6")
    command = argv[image_at + 1 :]
    assert command[:5] == ["timeout", "--kill-after=10", "120", "/bin/sh", "-c"]
    script = command[5]
    assert len(command) == 6
    pytest_at = script.index("-m pytest")
    assert "--junitxml=/work/.dtl/junit.xml" in script
    for index, path in enumerate((TEST_FILE, "src/pkg/mod.py")):
        for gate, gate_argv in CODE_GATE_COMMANDS:
            line = shlex.join(["/opt/dtl-venv/bin/python", *gate_argv, path])
            assert line in script
            assert script.index(line) > pytest_at
            assert gate_output_file(index, gate, "out") in script
            assert gate_output_file(index, gate, "exit") in script
    # The container's exit code is still pytest's.
    assert script.rstrip().endswith('exit "$rc"')


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.py",
        "/abs/path.py",
        "src/pkg/mod.txt",
        "src/pkg/$(reboot).py",
        "src/pkg/a b.py",
        "src/pkg/mod.py;rm",
    ],
)
def test_a_gate_path_that_could_escape_or_inject_is_refused(bad: str) -> None:
    with pytest.raises((DtlInvocationRefusedError, ValueError)):
        _spec(gate_paths=(bad,))


def test_at_most_four_gate_paths() -> None:
    with pytest.raises(ValueError, match="at most 4"):
        _spec(gate_paths=tuple(f"src/pkg/m{i}.py" for i in range(5)))


def test_the_request_refuses_a_gate_path_it_would_refuse_on_the_container() -> None:
    with pytest.raises(ValueError, match="not a repository Python file"):
        _request(gate_paths=("src/pkg/$(x).py",))
    assert _request(gate_paths=(TEST_FILE,)).gate_paths == (TEST_FILE,)


def _gate_files(exit_codes: dict[str, str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for gate, _ in CODE_GATE_COMMANDS:
        out = gate_output_file(0, gate, "out").removeprefix("/work/")
        files[out] = f"{gate} said something about {TEST_FILE}\n"
        if gate in exit_codes:
            files[gate_output_file(0, gate, "exit").removeprefix("/work/")] = (
                exit_codes[gate]
            )
    return files


def test_the_receipt_carries_each_gate_exit_code_and_output() -> None:
    client = FakeClient()
    client.files.update(
        _gate_files({"ruff_check": "1\n", "ruff_format": "0\n", "mypy": "0\n"})
    )
    receipt = HandlerFocusedTestRunEffect(client).run_sync(
        _request(gate_paths=(TEST_FILE,))
    )
    assert receipt.status is EnumFocusedTestRunStatus.COMPLETED
    assert [(g.path, g.gate, g.exit_code) for g in receipt.gate_outputs] == [
        (TEST_FILE, "ruff_check", 1),
        (TEST_FILE, "ruff_format", 0),
        (TEST_FILE, "mypy", 0),
    ]
    assert "ruff_check said something" in receipt.gate_outputs[0].output
    # Read back before teardown, never after.
    assert client.calls.index("run") < client.calls.index("teardown")


def test_a_gate_whose_exit_file_is_missing_is_reported_as_never_run() -> None:
    client = FakeClient()
    client.files.update(_gate_files({"ruff_check": "0\n", "ruff_format": "0\n"}))
    receipt = HandlerFocusedTestRunEffect(client).run_sync(
        _request(gate_paths=(TEST_FILE,))
    )
    by_gate = {g.gate: g.exit_code for g in receipt.gate_outputs}
    assert by_gate == {"ruff_check": 0, "ruff_format": 0, "mypy": None}


def test_no_gate_paths_means_no_gate_outputs() -> None:
    receipt = HandlerFocusedTestRunEffect(FakeClient()).run_sync(_request())
    assert receipt.status is EnumFocusedTestRunStatus.COMPLETED
    assert receipt.gate_outputs == ()


def test_request_default_has_no_gate_paths() -> None:
    assert ModelFocusedTestRunRequest.model_fields["gate_paths"].default == ()
