# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Guest steps are ssh to the tart ip; a guest step issued as `tart exec` fails here."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect import (
    runtime_onboarding_clean_vm_walk as runtime,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers import (
    handler_onboarding_clean_vm_walk as walk,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.ssh_tart_runner import (
    HandlerLocalTart,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    EnumWalkOutcome,
    EnumWalkStepKind,
    ModelRunnerResult,
    ModelWalkRequest,
    ModelWalkStep,
    StepNotDocumentedError,
)
from tests.nodes.node_onboarding_clean_vm_walk.test_clean_vm_walk import (
    STUB_ASKPASS,
    SUCCESS_LOG,
    guest_command,
    request,
)

pytestmark = pytest.mark.unit

TART_IP_OUTPUT = "192.0.2.201\n"
GUEST_KINDS = [k for k in EnumWalkStepKind if k.value.startswith("guest_")]


class SshOnlyRunner:
    """Records every argv and refuses a guest step issued through `tart exec`."""

    def __init__(self, tart_ip_output: str = TART_IP_OUTPUT) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.tart_ip_output = tart_ip_output
        self.bound: ModelWalkRequest | None = None

    def _kind(self, argv: Sequence[str]) -> EnumWalkStepKind:
        known = self.bound or walk.resolve_request(request(guest_ip=None))
        for kind in EnumWalkStepKind:
            try:
                expected = walk.build_step_argv(kind, known)
            except ValueError:  # a guest step before the tart ip is known
                continue
            if tuple(argv) == expected:
                return kind
        raise AssertionError(f"undocumented argv: {argv}")

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ModelRunnerResult:
        self.calls.append(tuple(argv))
        if argv[-1] == "--version":
            return ModelRunnerResult(exit_code=0, stdout="tart 2.0", stderr="")
        if len(argv) > 1 and argv[1] == "exec":
            raise AssertionError(f"guest step issued as tart exec: {argv}")
        kind = self._kind(argv)
        stdout = ""
        if kind == EnumWalkStepKind.TART_IP:
            stdout = self.tart_ip_output
            self.bound = walk.bind_guest_ip(
                walk.resolve_request(request(guest_ip=None)), stdout
            )
        if kind in (EnumWalkStepKind.GUEST_RUN_SCRIPT, EnumWalkStepKind.GUEST_READ_LOG):
            stdout = SUCCESS_LOG
        if kind in (
            EnumWalkStepKind.GUEST_DASHBOARD_PROBE_ROOT,
            EnumWalkStepKind.GUEST_DASHBOARD_PROBE_PROJECTIONS,
        ):
            stdout = "200 http://127.0.0.1:8123\n"
        return ModelRunnerResult(exit_code=0, stdout=stdout, stderr="")

    def start(self, argv: Sequence[str]) -> None:
        self.calls.append(tuple(argv))


def test_every_guest_step_is_ssh_to_the_tart_ip() -> None:
    runner = SshOnlyRunner()
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(
        request(guest_ip=None, vm_name="onex-walk-1234abcd")
    )
    assert receipt.outcome == EnumWalkOutcome.PASSED, receipt.failure_text
    guest_calls = [c for c in runner.calls if c[0] == "env"]
    assert len(guest_calls) == len(GUEST_KINDS)
    for argv in guest_calls:
        assert argv[1] == f"SSH_ASKPASS={STUB_ASKPASS}"
        assert argv[2] == "SSH_ASKPASS_REQUIRE=force"
        assert argv[3] == "ssh"
        options = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
        assert "StrictHostKeyChecking=no" in options
        assert "UserKnownHostsFile=/dev/null" in options
        assert any(o.startswith("ConnectTimeout=") for o in options)
        assert "BatchMode=yes" not in options
        assert argv[-2] == "admin@192.0.2.201"
        assert "exec" not in argv
    assert not [c for c in runner.calls if len(c) > 1 and c[1] == "exec"]
    assert [s.kind for s in receipt.steps if s.kind in GUEST_KINDS] == GUEST_KINDS
    assert receipt.vm_deleted


def test_guest_command_strings_are_unchanged_by_the_transport() -> None:
    req = walk.bind_guest_ip(walk.resolve_request(request()), TART_IP_OUTPUT)
    assert str(req.guest_ip) == "192.0.2.201"
    other = walk.bind_guest_ip(req, "192.0.2.9")
    for kind in GUEST_KINDS:
        assert guest_command(walk.build_step_argv(kind, req)) == guest_command(
            walk.build_step_argv(kind, other)
        )
        assert walk.build_step_argv(kind, req) != walk.build_step_argv(kind, other)


@pytest.mark.parametrize("kind", GUEST_KINDS)
def test_guard_refuses_the_tart_exec_form_of_a_guest_step(
    kind: EnumWalkStepKind,
) -> None:
    runner = SshOnlyRunner()
    req = walk.bind_guest_ip(walk.resolve_request(request()), TART_IP_OUTPUT)
    legacy = (
        req.tart_path or "",
        "exec",
        req.vm_name or "",
        "bash",
        "-lc",
        guest_command(walk.build_step_argv(kind, req)),
    )
    guard = walk.DocumentedStepGuard(req, runner)
    with pytest.raises(StepNotDocumentedError, match=kind.value):
        guard.issue(ModelWalkStep(kind=kind, argv=legacy, timeout_seconds=1))
    assert runner.calls == []


def test_runner_rejects_a_regression_to_tart_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: with the old builder restored, the stub runner fails the walk."""
    original = walk._documented_argv

    def tart_exec_again(
        kind: EnumWalkStepKind, req: ModelWalkRequest
    ) -> tuple[str, ...]:
        argv = original(kind, req)
        if kind.value.startswith("guest_"):
            return (req.tart_path or "", "exec", req.vm_name or "", *argv[-1:])
        return argv

    monkeypatch.setattr(walk, "_documented_argv", tart_exec_again)
    with pytest.raises(AssertionError, match="tart exec"):
        walk.HandlerOnboardingCleanVmWalk(SshOnlyRunner()).handle(
            request(guest_ip=None)
        )


def test_missing_askpass_fails_before_any_vm_is_cloned() -> None:
    runner = SshOnlyRunner()
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(
        request(guest_askpass_path=None)
    )
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert "guest_askpass_path" in (receipt.failure_text or "")
    assert receipt.steps == ()
    assert runner.calls == []


def test_unparseable_tart_ip_is_a_receipt_and_the_vm_is_deleted() -> None:
    runner = SshOnlyRunner(tart_ip_output="not an address\n")
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request(guest_ip=None))
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert receipt.vm_deleted
    assert not [s for s in receipt.steps if s.kind in GUEST_KINDS]


def test_local_transport_runs_the_same_commands_in_a_local_bash() -> None:
    runner = HandlerLocalTart()
    with patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        assert (
            runner.run(["~/.local/bin/tart", "--version"], timeout_seconds=2).stdout
            == "ok"
        )
        argv = run.call_args.args[0]
        assert argv[:2] == ["bash", "-c"]
        assert "ssh" not in argv[0]
        assert argv[2].startswith('"$HOME/"')
        runner.start(["~/.local/bin/tart", "run", "--no-graphics", "vm"])
        assert run.call_args.args[0][:2] == ["bash", "-c"]
        assert "nohup" in run.call_args.args[0][2]


def test_cli_selects_the_local_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    chosen: list[object] = []

    class Spy(walk.HandlerOnboardingCleanVmWalk):
        def __init__(self, runner: object = None) -> None:
            chosen.append(runner)
            super().__init__(SshOnlyRunner())

    monkeypatch.setattr(runtime, "HandlerOnboardingCleanVmWalk", Spy)
    monkeypatch.setattr(
        "sys.argv",
        [
            "walk",
            "--input",
            request(guest_ip=None, tart_transport="local").model_dump_json(),
        ],
    )
    assert runtime.main() == 0
    assert isinstance(chosen[0], HandlerLocalTart)
