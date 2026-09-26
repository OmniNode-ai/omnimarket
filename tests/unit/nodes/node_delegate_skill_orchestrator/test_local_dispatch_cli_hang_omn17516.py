# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17516: a caller deadline bounds the local in-memory dispatch path."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import multiprocessing
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution

_CHILD_MODE = "--omn17516-cli-receipt-runtime-child"
_CLI_TIMEOUT_SECONDS = 45
_CLI_HARD_BACKSTOP_SECONDS = _CLI_TIMEOUT_SECONDS + 10
# ``RuntimeLocal`` also has a cooperative wait of the CLI timeout.  On a spawn
# host that guard can race the port's supervised child startup, which would
# test the generic runtime timeout instead of the typed port terminal.  macOS
# uses ``spawn`` and the port's measured cold interpreter boot is about 18s;
# the former two-second caller budget expired before the hanging effect could
# possibly enter.  Forty-five seconds leaves real boot time for the effect,
# while the CLI's 55-second SIGALRM remains the later hard backstop.
_CHILD_RUNTIME_WAIT_SECONDS = 60
_CHILD_SUBPROCESS_TIMEOUT_SECONDS = 90
_CHILD_TERMINATION_GRACE_SECONDS = 5
_WORKTREE_ROOT = Path(__file__).resolve().parents[4]
_CORE_WORKTREE = _WORKTREE_ROOT.parent / "omnibase_core"
_INFRA_WORKTREE = _WORKTREE_ROOT.parent / "omnibase_infra"


class NeverReturningEffectHandler:
    """Pickleable effect that models an unresponsive local transport."""

    def __init__(self, child_entered_marker_path: Path) -> None:
        self._child_entered_marker_path = child_entered_marker_path

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self._child_entered_marker_path.write_text("entered\n", encoding="utf-8")
        del request
        while True:
            threading.Event().wait()


class _StageRecorder:
    """Persist parent-side effect-boundary stages for a bounded proof failure."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def mark(self, stage: str) -> None:
        with self._path.open("a", encoding="utf-8") as stage_file:
            stage_file.write(f"{time.monotonic():.6f} {stage}\n")


class _StageRecordingProcess:
    """Proxy the real process while recording the synchronous start boundary."""

    def __init__(self, process: Any, stages: _StageRecorder) -> None:
        self._process = process
        self._stages = stages

    def start(self) -> None:
        self._stages.mark("effect-process-start-enter")
        self._process.start()
        self._stages.mark("effect-process-start-return")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._process, name)


class _StageRecordingProcessContext:
    """Use the real multiprocessing context with observable construction stages."""

    def __init__(self, context: Any, stages: _StageRecorder) -> None:
        self._context = context
        self._stages = stages

    def Queue(self) -> Any:  # noqa: N802 - multiprocessing context protocol
        self._stages.mark("effect-queue-create")
        return self._context.Queue()

    def Process(  # noqa: N802 - multiprocessing context protocol
        self, *args: Any, **kwargs: Any
    ) -> _StageRecordingProcess:
        self._stages.mark("effect-process-construct")
        return _StageRecordingProcess(
            self._context.Process(*args, **kwargs), self._stages
        )


def _terminate_candidate_process_group(process: subprocess.Popen[str]) -> None:
    """Stop only the isolated proof child's session after its test bound expires."""

    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=_CHILD_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=_CHILD_TERMINATION_GRACE_SECONDS)


def _run_isolated_proof_child(
    command: list[str], *, stage_path: Path
) -> subprocess.CompletedProcess[str]:
    """Run the overlay in its own session so a timeout cannot strand descendants."""

    process = subprocess.Popen(
        command,
        cwd=_WORKTREE_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=_CHILD_SUBPROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _terminate_candidate_process_group(process)
        stage_text = (
            stage_path.read_text(encoding="utf-8") if stage_path.exists() else "<none>"
        )
        raise AssertionError(
            "isolated CLI proof child exceeded its bounded candidate session; "
            f"stages:\n{stage_text}"
        ) from error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _find_receipt(stdout: str) -> dict[str, object]:
    """Extract receipt-mode's one JSON receipt from the captured stdout."""
    decoder = json.JSONDecoder()
    receipts: list[dict[str, object]] = []
    for offset, character in enumerate(stdout):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(stdout[offset:])
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict):
            continue
        if isinstance(candidate.get("result"), dict) and "status" in candidate:
            receipts.append(candidate)
    assert len(receipts) == 1, (
        "receipt mode must emit exactly one structured receipt; "
        f"found {len(receipts)} in stdout={stdout!r}"
    )
    return receipts[0]


def _run_cli_receipt_runtime_child(proof_path: Path) -> None:
    """Drive the real cross-repo CLI chain inside an isolated editable overlay.

    The normal omnimarket environment deliberately pins released core/infra
    packages, while the normal infra environment deliberately has no market
    dependency.  This child makes the three OMN-17516 worktrees explicit
    editable inputs to ``uv``; it never uses ``PYTHONPATH`` or mutates a shared
    worktree venv.  The only injected boundary is the blocking effect itself,
    plus its temporary SQLite evidence target.
    """
    stage_path = proof_path.with_name("stages.log")
    stages = _StageRecorder(stage_path)
    stages.mark("child-enter")

    from omnibase_infra.cli import cli_delegate

    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        port_selection,
    )

    stages.mark("child-imports-complete")

    evidence_path = proof_path.with_name("delegation.sqlite")
    child_entered_marker_path = proof_path.with_name("effect-child-entered.marker")
    selected_deadlines: list[float] = []
    selected_ports: list[LocalDelegationDispatchPort] = []

    real_process_context = port_module._resolve_effect_process_context()
    port_module._resolve_effect_process_context = lambda: _StageRecordingProcessContext(
        real_process_context,
        stages,
    )

    def _select_local_port(
        event_bus: object,
        *,
        caller_deadline_monotonic: float | None = None,
    ) -> LocalDelegationDispatchPort:
        del event_bus
        assert caller_deadline_monotonic is not None, (
            "RuntimeLocal did not inject the authoritative CLI deadline into "
            "HandlerDelegateSkill"
        )
        stages.mark("local-port-selection")
        selected_deadlines.append(caller_deadline_monotonic)
        port = LocalDelegationDispatchPort(
            effect_handler=NeverReturningEffectHandler(child_entered_marker_path),
            evidence_db_path=evidence_path,
            # The caller deadline itself must force the supervised child path;
            # otherwise this test would merely restate the historical default.
            effect_process_boundary=False,
            caller_deadline_monotonic=caller_deadline_monotonic,
        )
        selected_ports.append(port)
        stages.mark("local-port-constructed")
        return port

    # The real handler resolves this factory lazily in its constructor, so this
    # preserves CLI -> receipt mode -> RuntimeLocal -> HandlerDelegateSkill ->
    # LocalDelegationDispatchPort while keeping evidence scoped to tmp_path.
    port_selection.select_delegation_dispatch_port = _select_local_port
    delegation_backend_resolution.load_bifrost_backends = lambda **_: [
        {
            "backend_id": "local-coder",
            "endpoint_url": "http://unreachable.invalid:8000/v1/chat/completions",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 65536,
            # The caller deadline, not a small transport timeout, must cause
            # this terminal.
            "timeout_ms": 300000,
            "capabilities": ["code_generation"],
        }
    ]
    cli_delegate._write_local_run_files = lambda **_: None

    real_run_receipt_mode = cli_delegate.run_receipt_mode
    observed_cli_timeout: list[int] = []

    def _run_receipt_mode_with_nonracing_runtime_wait(**kwargs: Any) -> int:
        timeout = kwargs.pop("timeout")
        assert timeout == _CLI_TIMEOUT_SECONDS
        observed_cli_timeout.append(timeout)
        return real_run_receipt_mode(
            timeout=_CHILD_RUNTIME_WAIT_SECONDS,
            **kwargs,
        )

    cli_delegate.run_receipt_mode = _run_receipt_mode_with_nonracing_runtime_wait

    baseline_children = {
        process.pid for process in multiprocessing.active_children() if process.pid
    }
    captured_stdout = contextlib.redirect_stdout
    stdout = io.StringIO()
    started = time.monotonic()
    stages.mark("before-cli-delegate")
    with captured_stdout(stdout):
        exit_code = cli_delegate.run_delegate(
            prompt="prove deadline propagation through the local CLI path",
            task_type="code_generation",
            max_tokens=128,
            source="external-client",
            bus="inmemory",
            state_root=proof_path.parent / "state",
            timeout=_CLI_TIMEOUT_SECONDS,
            verbose=False,
            emit_socket=proof_path.parent / "emit.sock",
            # The overlay itself names all participating source trees, so there
            # is no canonical install for the drift guard to reconcile/mutate.
            omni_home=None,
        )
    elapsed = time.monotonic() - started
    stages.mark("after-cli-delegate")

    receipt = _find_receipt(stdout.getvalue())
    result = receipt["result"]
    assert isinstance(result, dict)
    attempts = result.get("attempts")
    assert isinstance(attempts, list)
    assert attempts
    first_attempt = attempts[0]
    assert isinstance(first_attempt, dict)

    assert observed_cli_timeout == [_CLI_TIMEOUT_SECONDS]
    assert len(selected_deadlines) == 1
    assert len(selected_ports) == 1
    assert selected_ports[0]._caller_deadline_monotonic == selected_deadlines[0]
    assert 0 < selected_deadlines[0] - started <= _CLI_TIMEOUT_SECONDS + 0.5
    assert child_entered_marker_path.read_text(encoding="utf-8") == "entered\n", (
        "the timeout occurred before the deliberately hanging effect child entered; "
        "this is not proof of child cleanup"
    )
    assert exit_code == 1
    assert elapsed < _CLI_HARD_BACKSTOP_SECONDS
    assert result["status"] == "failed"
    assert first_attempt["failure_class"] == "timeout"
    assert "did not return within" in str(first_attempt["error_message"])

    with sqlite3.connect(str(evidence_path)) as connection:
        rows = connection.execute(
            "SELECT correlation_id, quality_gate_passed, error_message "
            "FROM delegation_events"
        ).fetchall()
    assert len(rows) == 1
    evidence_correlation_id, evidence_quality_gate_passed, evidence_error = rows[0]
    assert evidence_correlation_id
    assert evidence_quality_gate_passed == 0
    assert "did not return within" in str(evidence_error)

    orphan_pids = sorted(
        process.pid
        for process in multiprocessing.active_children()
        if process.pid not in baseline_children and process.pid and process.is_alive()
    )
    assert not orphan_pids

    proof_path.write_text(
        json.dumps(
            {
                "exit_code": exit_code,
                "elapsed_seconds": elapsed,
                "selection_deadline_remaining_seconds": selected_deadlines[0] - started,
                "child_entered_marker": child_entered_marker_path.exists(),
                "stages": stage_path.read_text(encoding="utf-8"),
                "receipt": receipt,
                "evidence_quality_gate_passed": evidence_quality_gate_passed,
                "orphan_pids": orphan_pids,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


@pytest.mark.integration
@pytest.mark.slow
def test_cli_receipt_runtime_deadline_reaches_typed_timeout_before_backstop(
    tmp_path: Path,
) -> None:
    """Real CLI chain: deadline reaches local dispatch before SIGALRM fires.

    This is intentionally an integration test, not a normal market unit test:
    it requires the dirty core, infra, and market implementations to be loaded
    together.  ``uv --isolated --with-editable`` provides that composition
    without an ambient import override or a shared-venv install.
    """
    proof_path = tmp_path / "proof.json"
    stage_path = tmp_path / "stages.log"
    command = [
        "uv",
        "run",
        "--isolated",
        "--with-editable",
        str(_CORE_WORKTREE),
        "--with-editable",
        str(_INFRA_WORKTREE),
        "--with-editable",
        str(_WORKTREE_ROOT),
        "python",
        "-m",
        __name__,
        _CHILD_MODE,
        str(proof_path),
    ]
    completed = _run_isolated_proof_child(command, stage_path=stage_path)
    if completed.returncode != 0:
        pytest.fail(
            "isolated CLI proof child failed:\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    assert proof_path.exists(), "CLI proof child exited without durable proof"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert "effect-process-start-return" in proof["stages"]
    assert proof["child_entered_marker"] is True
    assert proof["exit_code"] == 1
    assert proof["elapsed_seconds"] < _CLI_HARD_BACKSTOP_SECONDS
    assert proof["receipt"]["result"]["status"] == "failed"
    assert proof["receipt"]["result"]["attempts"][0]["failure_class"] == "timeout"
    assert proof["evidence_quality_gate_passed"] == 0
    assert proof["orphan_pids"] == []


@pytest.mark.unit
@pytest.mark.parametrize("effect_process_boundary", [True, False])
def test_caller_deadline_bounds_inmemory_dispatch_and_projects_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effect_process_boundary: bool,
) -> None:
    """A short caller deadline wins over the backend's five-minute timeout."""
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: [
            {
                "backend_id": "local-coder",
                "endpoint_url": "http://unreachable.invalid:8000/v1/chat/completions",
                "model_name": "Qwen3.6-35B-A3B",
                "tier": "local",
                "max_tokens": 65536,
                "timeout_ms": 300000,
                "capabilities": ["code_generation"],
            }
        ],
    )
    monkeypatch.setattr(port_module, "_DISPATCH_TIMEOUT_BUFFER_SECONDS", 0.2)

    correlation_id = uuid4()
    caller_deadline = time.monotonic() + 0.8
    child_entered_marker_path = tmp_path / (
        f"effect-child-entered-{effect_process_boundary}.marker"
    )
    port = LocalDelegationDispatchPort(
        effect_handler=NeverReturningEffectHandler(child_entered_marker_path),
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=effect_process_boundary,
        caller_deadline_monotonic=caller_deadline,
    )
    existing_child_pids = {process.pid for process in multiprocessing.active_children()}

    async def _run() -> dict[str, object]:
        return await port.dispatch(
            prompt="bounded local dispatch",
            task_type="code_generation",
            correlation_id=correlation_id,
            max_tokens=128,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )

    started = time.monotonic()
    result = asyncio.run(_run())
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert result["status"] == "failed"
    assert "did not return within" in str(result["error_message"])
    assert child_entered_marker_path.read_text(encoding="utf-8") == "entered\n"
    assert not {
        process.pid
        for process in multiprocessing.active_children()
        if process.pid not in existing_child_pids and process.is_alive()
    }

    with sqlite3.connect(str(tmp_path / "delegation.sqlite")) as connection:
        row = connection.execute(
            "SELECT quality_gate_passed FROM delegation_events "
            "WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()

    assert row == (0,)


@pytest.mark.unit
def test_expired_caller_deadline_projects_typed_timeout_without_effect_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired budget cannot trigger invalid zero-second request validation."""
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: [
            {
                "backend_id": "local-coder",
                "endpoint_url": "http://unreachable.invalid:8000/v1/chat/completions",
                "model_name": "Qwen3.6-35B-A3B",
                "tier": "local",
                "max_tokens": 65536,
                "timeout_ms": 300000,
                "capabilities": ["code_generation"],
            }
        ],
    )
    correlation_id = uuid4()
    port = LocalDelegationDispatchPort(
        evidence_db_path=tmp_path / "delegation.sqlite",
        caller_deadline_monotonic=time.monotonic() - 1.0,
    )

    result = asyncio.run(
        port.dispatch(
            prompt="already expired",
            task_type="code_generation",
            correlation_id=correlation_id,
            max_tokens=128,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )

    assert result["status"] == "failed"
    assert "deadline expired before effect started" in str(result["error_message"])
    with sqlite3.connect(str(tmp_path / "delegation.sqlite")) as connection:
        row = connection.execute(
            "SELECT quality_gate_passed FROM delegation_events "
            "WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()
    assert row == (0,)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != _CHILD_MODE:
        raise SystemExit(f"usage: python -m {__name__} {_CHILD_MODE} <proof-path>")
    _run_cli_receipt_runtime_child(Path(sys.argv[2]))
