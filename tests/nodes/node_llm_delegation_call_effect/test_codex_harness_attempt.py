# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Codex attempt process boundary, receipt accounting and sandbox policy."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_llm_delegation_call_effect import (
    HandlerCodexHarnessAttempt,
    ModelCodexHarnessPolicy,
    ModelCodexHarnessRequest,
)


def engine(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "engine"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o700)
    return path


def request(timeout: float = 3) -> ModelCodexHarnessRequest:
    return ModelCodexHarnessRequest(
        prompt="fixture input", model="fixture-model", timeout_seconds=timeout
    )


def test_runner_launch_and_measured_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", "/existing/account")
    monkeypatch.setenv("HOST_RUNTIME_MARKER", "retain")
    binary = engine(
        tmp_path,
        """import json, os, pathlib, sys
args = sys.argv[1:]
assert sys.stdin.read() == "fixture input"
assert args[:1] == ["exec"]
assert args[args.index("-m") + 1] == "fixture-model"
assert args[args.index("-s") + 1] == "workspace-write"
assert 'approval_policy="never"' in args
assert 'sandbox_workspace_write.network_access=true' in args
assert "--ignore-user-config" not in args and "--ignore-rules" not in args
assert os.environ["CODEX_HOME"] == "/existing/account"
assert os.environ["HOST_RUNTIME_MARKER"] == "retain"
pathlib.Path(args[args.index("-o") + 1]).write_text("fixture answer")
print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":42,"output_tokens":7,"cached_input_tokens":12}}))
""",
    )
    result = HandlerCodexHarnessAttempt(
        ModelCodexHarnessPolicy(executable=str(binary), workdir=tmp_path)
    ).handle(request())
    assert result.answer == "fixture answer"
    assert (result.tokens_in, result.tokens_out, result.tokens_cached) == (42, 7, 12)
    assert result.exit_cause == "completed"
    assert result.exit_code == 0
    assert result.model == "fixture-model"
    assert result.wall_seconds > 0
    assert not list(tmp_path.glob("codex-attempt-*"))


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (None, (None, None, None)),
        ({"input_tokens": 0, "output_tokens": 0}, (0, 0, None)),
    ],
)
def test_unknown_usage_is_not_zero(
    tmp_path: Path, usage: dict[str, int] | None, expected: tuple[int | None, ...]
) -> None:
    binary = engine(
        tmp_path,
        f"""import json, pathlib, sys
args=sys.argv
pathlib.Path(args[args.index("-o")+1]).write_text("answer")
print(json.dumps({{"type":"turn.completed", "usage":{usage!r}}}))
""",
    )
    result = HandlerCodexHarnessAttempt(
        ModelCodexHarnessPolicy(executable=str(binary), workdir=tmp_path)
    ).handle(request())
    assert (result.tokens_in, result.tokens_out, result.tokens_cached) == expected
    assert result.exit_cause == "completed"


@pytest.mark.parametrize(
    ("body", "cause"),
    [
        (
            'import sys; sys.stderr.write("Error: failed to initialize in-process app-server client: Operation not permitted (os error 1)"); sys.exit(1)',
            "startup_refused",
        ),
        (
            'import sys; sys.stderr.write("private diagnostic"); sys.exit(2)',
            "process_failed",
        ),
        ('print("not json")', "invalid_output"),
        ('import json; print(json.dumps({"type":"turn.failed"}))', "turn_failed"),
    ],
)
def test_failed_attempts_have_typed_private_receipts(
    tmp_path: Path, body: str, cause: str
) -> None:
    binary = engine(tmp_path, body)
    result = HandlerCodexHarnessAttempt(
        ModelCodexHarnessPolicy(executable=str(binary), workdir=tmp_path)
    ).handle(request())
    assert result.exit_cause == cause
    assert result.tokens_in is None
    assert result.answer == ""
    assert "private diagnostic" not in result.model_dump_json()
    assert "fixture input" not in result.model_dump_json()


def test_timeout_ends_attempt_process_group(tmp_path: Path) -> None:
    binary = engine(
        tmp_path,
        """import os, pathlib, subprocess, sys, time
child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])
pathlib.Path("child.pid").write_text(str(child.pid))
time.sleep(30)
""",
    )
    result = HandlerCodexHarnessAttempt(
        ModelCodexHarnessPolicy(
            executable=str(binary), workdir=tmp_path, terminate_grace_seconds=0.1
        )
    ).handle(request(0.3))
    assert result.exit_cause == "timeout"
    assert result.wall_seconds < 5
    # The descendant must not retain stdout/stderr and hold communicate open.
    assert result.tokens_in is None


def test_missing_executable_is_receipted(tmp_path: Path) -> None:
    result = HandlerCodexHarnessAttempt(
        ModelCodexHarnessPolicy(executable=str(tmp_path / "absent"), workdir=tmp_path)
    ).handle(request())
    assert result.exit_cause == "launch_failed"
    assert result.exit_code is None


@pytest.mark.parametrize(
    "override",
    [
        {"sandbox": "danger-full-access"},
        {"approval_policy": "on-request"},
        {"network_access": False, "executable": ""},
    ],
)
def test_policy_rejects_invalid_configuration(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelCodexHarnessPolicy.model_validate(override)


def test_packaged_contract_policy_and_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = HandlerCodexHarnessAttempt().policy
    assert baseline.sandbox == "workspace-write"
    assert baseline.approval_policy == "never"
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "codex_harness:\n  max_timeout_seconds: 10\n  reasoning_effort: low\n"
    )
    monkeypatch.setenv("CODEX_HARNESS_OVERLAY_PATH", str(overlay))
    handler = HandlerCodexHarnessAttempt()
    assert handler.policy.max_timeout_seconds == 10
    assert handler.policy.reasoning_effort == "low"
    with pytest.raises(ValueError, match="timeout"):
        handler.handle(request(11))


def test_public_surface_imports() -> None:
    from importlib.metadata import entry_points

    entry = next(
        e
        for e in entry_points(group="onex.nodes")
        if e.name == "node_llm_delegation_call_effect"
    )
    module = entry.load()
    assert module.HandlerCodexHarnessAttempt is HandlerCodexHarnessAttempt


def test_cli_stdin_and_receipt(tmp_path: Path) -> None:
    import subprocess

    binary = engine(
        tmp_path,
        """import json, pathlib, sys
args=sys.argv
assert sys.stdin.read() == "fixture input"
pathlib.Path(args[args.index("-o")+1]).write_text("fixture answer")
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":2,"output_tokens":1,"cached_input_tokens":0}}))
""",
    )
    overlay = tmp_path / "policy.yaml"
    overlay.write_text(
        f"codex_harness:\n  executable: {binary}\n  workdir: {tmp_path}\n"
    )
    import json
    import os

    environment = dict(os.environ) | {"CODEX_HARNESS_OVERLAY_PATH": str(overlay)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_llm_delegation_call_effect",
            "codex-harness",
            "--model",
            "fixture-model",
            "--timeout",
            "3",
        ],
        input="fixture input",
        capture_output=True,
        text=True,
        env=environment,
        timeout=20,
    )
    assert result.returncode == 0
    receipt = json.loads(result.stdout)
    assert receipt["answer"] == "fixture answer"
    assert receipt["tokens_cached"] == 0
    assert receipt["model_source"] == "requested"
    assert receipt["exit_cause"] == "completed"
    assert "fixture input" not in result.stdout + result.stderr


def test_invalid_request_never_echoes_prompt() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as error:
        ModelCodexHarnessRequest.model_validate(
            {
                "prompt": ["private input"],
                "model": "fixture-model",
                "timeout_seconds": 3,
            }
        )
    assert "private input" not in str(error.value)


@pytest.mark.parametrize("value", [-1, True, "12", 1.5])
def test_bad_usage_is_unknown(tmp_path: Path, value: object) -> None:
    binary = engine(
        tmp_path,
        f"""import json, pathlib, sys
args=sys.argv
pathlib.Path(args[args.index("-o")+1]).write_text("answer")
print(json.dumps({{"type":"turn.completed", "usage":{{"input_tokens":{value!r}}}}}))
""",
    )
    result = HandlerCodexHarnessAttempt(
        ModelCodexHarnessPolicy(executable=str(binary), workdir=tmp_path)
    ).handle(request())
    assert result.tokens_in is None


def test_relative_workdir_is_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert ModelCodexHarnessPolicy(workdir=Path(".")).workdir == tmp_path.resolve()


def test_unreadable_answer_is_receipted(tmp_path: Path) -> None:
    binary = engine(
        tmp_path,
        """import json, pathlib, sys
args=sys.argv
pathlib.Path(args[args.index("-o")+1]).write_bytes(b"\\xff")
print(json.dumps({"type":"turn.completed"}))
""",
    )
    result = HandlerCodexHarnessAttempt(
        ModelCodexHarnessPolicy(executable=str(binary), workdir=tmp_path)
    ).handle(request())
    assert result.exit_cause == "invalid_output"
