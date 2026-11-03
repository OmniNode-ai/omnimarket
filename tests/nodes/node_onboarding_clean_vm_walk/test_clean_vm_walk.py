# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Documented commands, typed receipts and real onboarding log formats (no SSH)."""

from __future__ import annotations

import importlib
import json
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect import (
    runtime_onboarding_clean_vm_walk as runtime,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers import (
    handler_onboarding_clean_vm_walk as walk,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.ssh_tart_runner import (
    HandlerSshTart,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.walk_log_parser import (
    parse_delegated_run,
    parse_phases,
    parse_pins,
    seconds_to_first_delegated_run,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    EnumPhaseResult,
    EnumWalkOutcome,
    EnumWalkStepKind,
    ModelDashboardResult,
    ModelDelegatedRun,
    ModelInstalledPin,
    ModelPhaseRecord,
    ModelRunnerResult,
    ModelVmSpec,
    ModelWalkReceipt,
    ModelWalkRequest,
    ModelWalkStep,
    ModelWalkStepRecord,
    StepNotDocumentedError,
)

pytestmark = pytest.mark.unit

# These failure lines preserve the supplied first-walk excerpt, with the home directory generalized.
FAILED_LOG = """21:04:03 OmniNode developer onboarding (local runtime). Status file: /var/folders/p7/T/omninode-onboarding/status
21:04:03 ▶ Phase 0/6: Preflight (reads this Mac; changes nothing you have not agreed to)  (usually under a minute)
  macOS                  26.6.2           14+            14+
21:04:04 ✔ Phase 0/6 PASSED: Preflight (reads this Mac; changes nothing you have not agreed to) (1s) -- native onex.
21:04:04 ▶ Phase 1/6: Base tools (Xcode tools, Homebrew, gh, jq, python@3.13, uv)  (usually 5-20 minutes on a clean Mac)
21:07:12 ✔ Phase 1/6 PASSED: Base tools (Xcode tools, Homebrew, gh, jq, python@3.13, uv) (3m 08s)
21:07:12 ▶ Phase 2/6: Workspace (/srv/walker/Code/omnibase)  (usually 2-5 minutes)
21:07:58 ✔ Phase 2/6 PASSED: Workspace (/srv/walker/Code/omnibase) (46s)
21:07:58 ▶ Phase 3/6: onex, local identity and your models  (usually 3-10 minutes, more when Ollama downloads a model)
21:09:06 $ one onex delegate (attempt 1/3; output not logged)
identity: host=Manageds-Virtual-Machine.local locus=venv:.../omnibase/.onex-dispatch-venv omnibase_compat=0.5.7@registry omnibase_core=0.47.50@registry omnibase_infra=0.38.76@local_path omnibase_spi=0.23.7@registry omnimarket=0.4.319@370f405
onex delegate failed: provider_error: [ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT] delegation.customer_provider_key.absent: delegation refused ...
{"skill_name":"node_delegate_skill_orchestrator","node_name":"node_delegate_skill_orchestrator","status":"failed","correlation_id":"17929dbf-1da7-4420-87d1-33cc0f8a0ccb","run_id":"8fee00c1-b313-4100-8fc0-953057f184de","exit_code":1,"duration_ms":1042,"result":{"workflow_result":"failed","exit_code":1,"terminal_payload":{"status":"failed","task_type":"document"}}}
delegate artifacts (route UNATTRIBUTED -- no backend was reached: this run was refused before any rung was dispatched): /srv/walker/.onex_state/runs/8fee00c1/result.txt
21:09:28 ✘ Phase 3/6 FAILED: onex, local identity and your models (1m 30s)
21:09:28   Step:      one onex delegate (after 3 attempt(s))
21:09:28   Last error: delegate artifacts (route UNATTRIBUTED -- no backend was reached ...)
"""
SUCCESS_LOG = (
    FAILED_LOG[: FAILED_LOG.index("onex delegate failed:")]
    + '{"skill_name":"node_delegate_skill_orchestrator","status":"success",'
    '"run_id":"success-1","result":{"terminal_payload":{"backend":"local"}}}\n'
    "21:09:28 ✔ Phase 3/6 PASSED: onex, local identity and your models (1m 30s)\n"
    "21:09:28 ▶ Phase 4/6: Plugins  (usually under a minute)\n"
    "21:09:30 ✔ Phase 4/6 PASSED: Plugins (2s)\n"
    "21:09:30 ▶ Phase 5/6: Verification\n"
    "21:09:31 ✔ Phase 5/6 PASSED: Verification (1s)\n"
    "21:09:31 ▶ Phase 6/6: Complete\n"
    "21:09:32 ✔ Phase 6/6 PASSED: Complete (1s)\n"
)
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_onboarding_clean_vm_walk_effect/contract.yaml"
)


STUB_GUEST_IP = "192.0.2.77"
STUB_ASKPASS = "/opt/walk/askpass"


def guest_command(argv: Sequence[str]) -> str:
    """The unchanged guest command inside the ssh argv's `bash -lc '<command>'`."""
    shell, flag, command = shlex.split(argv[-1])
    assert (shell, flag) == ("bash", "-lc")
    return command


def request(**overrides: object) -> ModelWalkRequest:
    return ModelWalkRequest.model_validate(
        {
            "script_sha": "a" * 40,
            "checksum_line": "b" * 64 + "  lab-onboarding.sh",
            "providers": ["local"],
            "local_url": "http://127.0.0.1:8000/v1",
            "local_model": "test model",
            "tart_host": "192.0.2.1",
            "vm_name": "onex-walk-1234abcd",
            "guest_askpass_path": STUB_ASKPASS,
            "guest_ip": STUB_GUEST_IP,
            **overrides,
        }
    )


class StubTartRunner:
    """Scripted Tart transport; nothing leaves the test process."""

    def __init__(self, log: str = SUCCESS_LOG) -> None:
        self.log = log
        self.calls: list[tuple[str, ...]] = []
        self.scripted: dict[EnumWalkStepKind, list[ModelRunnerResult]] = {}
        self.version = ModelRunnerResult(exit_code=0, stdout="tart 2.0", stderr="")
        self.request = walk.resolve_request(request())

    def kind(self, argv: Sequence[str]) -> EnumWalkStepKind:
        return next(
            kind
            for kind in EnumWalkStepKind
            if tuple(argv) == walk.build_step_argv(kind, self.request)
        )

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ModelRunnerResult:
        self.calls.append(tuple(argv))
        if argv[-1] == "--version":
            return self.version
        kind = self.kind(argv)
        if self.scripted.get(kind):
            return self.scripted[kind].pop(0)
        stdout = ""
        if kind == EnumWalkStepKind.TART_IP:
            stdout = f"{STUB_GUEST_IP}\n"
        if kind in (EnumWalkStepKind.GUEST_RUN_SCRIPT, EnumWalkStepKind.GUEST_READ_LOG):
            stdout = self.log
        if kind in (
            EnumWalkStepKind.GUEST_DASHBOARD_PROBE_ROOT,
            EnumWalkStepKind.GUEST_DASHBOARD_PROBE_PROJECTIONS,
        ):
            stdout = "200 http://127.0.0.1:8123\n"
        return ModelRunnerResult(exit_code=0, stdout=stdout, stderr="")

    def start(self, argv: Sequence[str]) -> None:
        self.calls.append(tuple(argv))


def test_step_outside_documented_list_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = StubTartRunner()
    guard = walk.DocumentedStepGuard(runner.request, runner)
    original = walk.build_step_argv
    valid = original(EnumWalkStepKind.GUEST_RUN_SCRIPT, runner.request)
    with pytest.raises(StepNotDocumentedError, match="not documented"):
        guard.issue(
            ModelWalkStep.model_construct(
                kind="hand_fix", argv=valid, timeout_seconds=1
            )
        )
    with pytest.raises(StepNotDocumentedError, match="guest_run_script"):
        guard.issue(
            ModelWalkStep(
                kind=EnumWalkStepKind.GUEST_RUN_SCRIPT,
                argv=(*valid[:-1], "brew install tart"),
                timeout_seconds=1,
            )
        )
    assert runner.calls == []

    def tampered(kind: EnumWalkStepKind, req: ModelWalkRequest) -> tuple[str, ...]:
        argv = original(kind, req)
        if kind == EnumWalkStepKind.GUEST_RUN_SCRIPT:
            return (*argv[:-1], argv[-1] + "; rm -rf /tmp/undocumented")
        return argv

    monkeypatch.setattr(walk, "build_step_argv", tampered)
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request())
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert "guest_run_script" in (receipt.failure_text or "")
    assert receipt.vm_deleted
    assert receipt.hand_fix_steps == ("guest_run_script",)


def test_every_issued_step_is_documented() -> None:
    runner = StubTartRunner()
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request())
    assert receipt.outcome == EnumWalkOutcome.PASSED, receipt.failure_text
    assert receipt.terminal_failure_cause is None
    assert runner.calls[0] == ("~/.local/bin/tart", "--version")
    kinds = [runner.kind(argv) for argv in runner.calls[1:]]
    assert kinds == list(EnumWalkStepKind)
    assert [s.kind for s in receipt.steps] == kinds
    for argv, step in zip(runner.calls[1:], receipt.steps, strict=True):
        assert argv == walk.build_step_argv(step.kind, runner.request)


def test_receipt_has_every_field_typed() -> None:
    receipt = walk.HandlerOnboardingCleanVmWalk(StubTartRunner()).handle(request())
    assert set(receipt.model_dump()) == {
        "outcome",
        "script_sha",
        "checksum_line",
        "providers",
        "local_url",
        "vm_spec",
        "phases",
        "steps",
        "total_seconds",
        "seconds_to_first_delegated_run",
        "delegated_run",
        "dashboard",
        "pins_installed",
        "omnibase_spi_version",
        "branches_installed",
        "vm_deleted",
        "failure_text",
        "terminal_failure_cause",
        "log_tail",
        "hand_fix_steps",
        "started_at",
        "finished_at",
    }
    assert isinstance(receipt.vm_spec, ModelVmSpec)
    assert (receipt.vm_spec.cpu, receipt.vm_spec.memory_gb) == (4, 8)
    assert receipt.vm_spec.image == "ghcr.io/cirruslabs/macos-tahoe-vanilla:latest"
    assert receipt.vm_spec.macos_version == "26.6.2"
    assert receipt.vm_spec.disk_gb is None
    assert len(receipt.phases) == 7
    assert [p.phase_number for p in receipt.phases] == list(range(7))
    assert all(isinstance(p, ModelPhaseRecord) for p in receipt.phases)
    assert all(p.result == EnumPhaseResult.PASSED for p in receipt.phases)
    assert all(isinstance(p.seconds, float) for p in receipt.phases)
    assert isinstance(receipt.seconds_to_first_delegated_run, float)
    assert receipt.seconds_to_first_delegated_run == 303.0
    assert isinstance(receipt.delegated_run, ModelDelegatedRun)
    assert receipt.delegated_run.backend == "local"
    assert receipt.delegated_run.status == "success"
    assert isinstance(receipt.dashboard, ModelDashboardResult)
    assert (
        receipt.dashboard.root_http_status
        == receipt.dashboard.projections_http_status
        == 200
    )
    assert receipt.dashboard.base_url == "http://127.0.0.1:8123"
    assert all(isinstance(p, ModelInstalledPin) for p in receipt.pins_installed)
    assert receipt.omnibase_spi_version == "0.23.7"
    assert all(isinstance(s, ModelWalkStepRecord) for s in receipt.steps)
    assert all(isinstance(s.seconds, float) for s in receipt.steps)
    assert isinstance(receipt.total_seconds, float)
    assert receipt.vm_deleted is True
    assert receipt.hand_fix_steps == ()
    assert receipt.started_at.endswith("+00:00")
    assert receipt.finished_at.endswith("+00:00")
    assert ModelWalkReceipt.model_validate_json(receipt.model_dump_json()) == receipt


def test_failed_phase_returns_receipt_not_exception() -> None:
    runner = StubTartRunner(FAILED_LOG)
    runner.scripted[EnumWalkStepKind.GUEST_RUN_SCRIPT] = [
        ModelRunnerResult(exit_code=1, stdout="", stderr="onboarding failed")
    ]
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request())
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert receipt.phases[3].result == EnumPhaseResult.FAILED
    assert "Last error" in (receipt.phases[3].failure_text or "")
    assert receipt.delegated_run.status == "failed"
    assert receipt.delegated_run.backend is None
    assert receipt.dashboard.failure_text.startswith("not attempted:")
    assert receipt.vm_deleted
    runner = StubTartRunner()
    runner.scripted[EnumWalkStepKind.TART_CLONE] = [
        ModelRunnerResult(exit_code=1, stdout="", stderr="clone refused")
    ]
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request())
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert (receipt.failure_text or "").startswith("tart_clone: exit 1: clone refused")
    assert receipt.terminal_failure_cause == receipt.failure_text
    # A refused clone created nothing: the VM under that name is not this walk's.
    assert not receipt.vm_deleted
    assert "was not created by this walk" in (receipt.failure_text or "")
    assert [runner.kind(argv) for argv in runner.calls[1:]] == [
        EnumWalkStepKind.TART_CLONE,
    ]
    runner = StubTartRunner()
    runner.version = ModelRunnerResult(exit_code=127, stdout="", stderr="missing")
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request())
    assert "tart not found at ~/.local/bin/tart on 192.0.2.1" in receipt.failure_text
    assert len(runner.calls) == 1


def test_checksum_mismatch_stops_before_running_script() -> None:
    runner = StubTartRunner()
    runner.scripted[EnumWalkStepKind.GUEST_VERIFY_CHECKSUM] = [
        ModelRunnerResult(exit_code=1, stdout="FAILED", stderr="")
    ]
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request())
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert "checksum mismatch" in (receipt.failure_text or "")
    assert "checksum mismatch" in (receipt.terminal_failure_cause or "")
    assert EnumWalkStepKind.GUEST_RUN_SCRIPT not in [s.kind for s in receipt.steps]
    assert receipt.vm_deleted


@pytest.mark.parametrize(
    "overrides",
    [
        {"script_sha": "xyz"},
        {"script_sha": "a" * 40 + "\n"},
        {"checksum_line": "b" * 64 + " lab-onboarding.sh"},
        {"checksum_line": "b" * 64 + "  lab-onboarding.sh\n"},
        {"providers": ["local"], "local_url": None},
        {"providers": ["local"], "local_model": None},
        {"providers": []},
        {"providers": ["unknown"]},
        {"cpu": 0},
        {"vm_name": "../bad"},
    ],
)
def test_request_validation(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(**overrides)


def test_parser_real_excerpts() -> None:
    phases = parse_phases(FAILED_LOG)
    assert [p.seconds for p in phases] == [1.0, 188.0, 46.0, 90.0]
    assert phases[0].name.endswith("agreed to)")
    assert phases[3].name == "onex, local identity and your models"
    assert "Step:" in phases[3].failure_text
    assert "Last error:" in phases[3].failure_text
    assert parse_delegated_run(FAILED_LOG).status == "failed"
    assert seconds_to_first_delegated_run(FAILED_LOG) is None
    assert parse_delegated_run(SUCCESS_LOG).backend == "local"
    assert seconds_to_first_delegated_run(SUCCESS_LOG) == 303.0
    pins = parse_pins(FAILED_LOG + "identity: omnibase_spi=9.0@registry\n")
    assert len(pins) == 1
    assert pins[0].version == "9.0"
    incomplete = parse_phases("23:59:59 ▶ Phase 0/6: Preflight\n")
    assert incomplete[0].result == EnumPhaseResult.SKIPPED
    assert incomplete[0].seconds is None


@pytest.mark.parametrize("key", ["backend", "provider", "backend_id", "route"])
def test_delegate_backend_and_midnight(key: str) -> None:
    payload = json.dumps(
        {
            "skill_name": "node_delegate_skill_orchestrator",
            "status": "success",
            "run_id": "run",
            "result": {"terminal_payload": {key: "ollama"}},
        },
        separators=(",", ":"),
    )
    log = "23:59:59 ▶ Phase 0/6: Preflight\n00:00:02 $ delegate\n" + payload
    assert parse_delegated_run(log).backend == "ollama"
    assert seconds_to_first_delegated_run(log) == 3.0
    fallback = log.replace(f'"{key}":"ollama"', '"task_type":"document"')
    assert (
        parse_delegated_run(
            fallback + "\ndelegate artifacts (route local): /tmp/result"
        ).backend
        == "local"
    )
    assert (
        parse_delegated_run(
            fallback + "\ndelegate artifacts (route UNATTRIBUTED -- refused)"
        ).backend
        is None
    )


def test_contract_declares_documented_steps_and_topics() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert [s["kind"] for s in contract["documented_steps"]] == list(EnumWalkStepKind)
    assert contract["tart"]["cpu"] == 4
    assert contract["tart"]["memory_gb"] == 8
    dispatch = contract["runtime_dispatch"]
    assert (
        dispatch["command_topic"]
        == "onex.cmd.omnimarket.onboarding-clean-vm-walk-requested.v1"
    )
    assert contract["event_bus"]["subscribe_topics"] == [dispatch["command_topic"]]
    assert set(contract["event_bus"]["publish_topics"]) == set(
        dispatch["terminal_events"].values()
    )
    assert (
        dispatch["terminal_events"]["success"]
        == "onex.evt.omnimarket.onboarding-clean-vm-walk-completed.v1"
    )
    assert (
        dispatch["terminal_events"]["failure"]
        == "onex.evt.omnimarket.onboarding-clean-vm-walk-failed.v1"
    )
    assert contract["terminal_event"] == dispatch["terminal_events"]["success"]
    assert contract["descriptor"]["timeout_ms"] == 3600000


def test_canonical_file_shape_binds_walk_handlers_without_scripts() -> None:
    """AC4 checks the new node's layout as well as the shared ratchet."""
    contract = yaml.safe_load(CONTRACT.read_text())
    handlers = (
        (contract["handler"], "class", walk.HandlerOnboardingCleanVmWalk),
        (
            contract["transport_bindings"]["tart_ssh"]["handler"],
            "name",
            HandlerSshTart,
        ),
    )
    for binding, class_key, expected_class in handlers:
        module = importlib.import_module(binding["module"])
        assert getattr(module, binding[class_key]) is expected_class
        assert module.__file__ is not None
        assert Path(module.__file__).resolve().parent == CONTRACT.parent / "handlers"
    assert not [
        path.relative_to(CONTRACT.parent)
        for path in CONTRACT.parent.rglob("*")
        if path.is_file() and path.suffix in {".sh", ".bash", ".zsh"}
    ]


def test_redaction() -> None:
    url = "http://127.0.0.1:8123/?token=abc123"
    runner = StubTartRunner(SUCCESS_LOG + url + "\ntoken=abc123\n")
    runner.scripted[EnumWalkStepKind.GUEST_DASHBOARD_PROBE_ROOT] = [
        ModelRunnerResult(exit_code=0, stdout=f"200 {url}", stderr="")
    ]
    req = request(local_url="http://127.0.0.1:8000/v1?token=abc123")
    runner.request = walk.resolve_request(req)
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(req)
    dumped = receipt.model_dump_json()
    assert url not in dumped
    assert "abc123" not in dumped
    assert receipt.dashboard.base_url == "http://127.0.0.1:8123"


def test_builders_quote_flags_and_use_login_shell() -> None:
    req = walk.resolve_request(
        request(providers=["local", "ollama"], ollama_model="qwen; echo oops")
    )
    for kind in EnumWalkStepKind:
        argv = walk.build_step_argv(kind, req)
        if kind.value.startswith("guest_"):
            assert argv[0] == "env"
            assert argv[3] == "ssh"
            assert argv[-2] == f"{req.guest_user}@{STUB_GUEST_IP}"
            assert "exec" not in argv
            assert guest_command(argv)
        else:
            assert argv[0] == "~/.local/bin/tart"
    command = guest_command(
        walk.build_step_argv(EnumWalkStepKind.GUEST_RUN_SCRIPT, req)
    )
    tokens = shlex.split(command)
    assert tokens[tokens.index("--provider") + 1] == "local,ollama"
    assert tokens[tokens.index("--ollama-model") + 1] == "qwen; echo oops"
    assert '"$HOME/walk-run.log"' in command
    assert walk.build_step_argv(EnumWalkStepKind.TART_RUN, req)[1:] == (
        "run",
        "--no-graphics",
        req.vm_name,
    )


def test_transport_uses_bash_home_expansion_timeout_and_detached_start() -> None:
    runner = HandlerSshTart("host")
    with patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        result = runner.run(["~/.local/bin/tart", "--version"], timeout_seconds=2)
        assert result.stdout == "ok"
        argv = run.call_args.args[0]
        assert argv[:8] == [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "host",
            "bash",
            "-c",
        ]
        assert "$HOME/" in shlex.split(argv[8])[0]
        assert run.call_args.kwargs["timeout"] == 2
        runner.start(["~/.local/bin/tart", "run", "--no-graphics", "vm"])
        assert "nohup" in run.call_args.args[0][-1]
        assert "/tmp/vm.tart.log" in run.call_args.args[0][-1]
        assert "&" in run.call_args.args[0][-1]
        run.side_effect = subprocess.TimeoutExpired("ssh", 2)
        assert runner.run(["tart", "ip", "vm"], timeout_seconds=2).exit_code == 124


def test_guest_readiness_retries_only_fetch() -> None:
    runner = StubTartRunner()
    runner.scripted[EnumWalkStepKind.GUEST_FETCH_SCRIPT] = [
        ModelRunnerResult(exit_code=1, stdout="", stderr="agent cannot connect"),
        ModelRunnerResult(exit_code=1, stdout="", stderr="connect failed"),
    ]
    with patch.object(walk.time, "sleep") as sleep:
        receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request())
    assert receipt.outcome == EnumWalkOutcome.PASSED
    assert sleep.call_count == 2
    assert sleep.call_args.args == (6,)
    assert [s.kind for s in receipt.steps].count(
        EnumWalkStepKind.GUEST_FETCH_SCRIPT
    ) == 3


@pytest.mark.parametrize(
    "kind", [EnumWalkStepKind.TART_STOP, EnumWalkStepKind.GUEST_DASHBOARD_PROBE_ROOT]
)
def test_failure_still_deletes_vm(kind: EnumWalkStepKind) -> None:
    runner = StubTartRunner()
    runner.scripted[kind] = [
        ModelRunnerResult(exit_code=1, stdout="", stderr="failure")
    ]
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request())
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert receipt.vm_deleted
    assert receipt.steps[-1].kind == EnumWalkStepKind.TART_DELETE


def test_total_clock_and_branches_are_receipt_only() -> None:
    runner = StubTartRunner()
    runner.request = walk.resolve_request(request(branches_installed=["fix/one"]))
    ticks = iter(float(n) for n in range(1000))
    receipt = walk.HandlerOnboardingCleanVmWalk(
        runner, clock=lambda: next(ticks)
    ).handle(runner.request)
    assert receipt.total_seconds > 0
    assert receipt.branches_installed == ("fix/one",)
    assert "fix/one" not in " ".join(" ".join(argv) for argv in runner.calls)


@pytest.mark.parametrize("projections", [False, True])
def test_dashboard_shell_preserves_auth_query_without_printing_it(
    tmp_path: Path,
    projections: bool,
) -> None:
    # Exercise the actual shell program with a shell-function curl: no HTTP requests.
    url = "http://127.0.0.1:8123/?token=abc123&session=secret"
    (tmp_path / "walk-dashboard.log").write_text(url + "\n")
    kind = (
        EnumWalkStepKind.GUEST_DASHBOARD_PROBE_PROJECTIONS
        if projections
        else EnumWalkStepKind.GUEST_DASHBOARD_PROBE_ROOT
    )
    command = guest_command(walk.build_step_argv(kind, walk.resolve_request(request())))
    prefix = 'curl() { printf "%s" "${!#}" > "$HOME/curl-target"; printf 200; }; '
    completed = subprocess.run(
        ["bash", "-c", prefix + command],
        env={"HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "200 http://127.0.0.1:8123\n"
    expected = (
        "http://127.0.0.1:8123/projections?token=abc123&session=secret"
        if projections
        else url
    )
    assert (tmp_path / "curl-target").read_text() == expected


def test_contract_defaults_are_resolved_at_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    contract["tart"].update(
        cpu=6, memory_gb=12, stock_image="stock:test", tart_path="/opt/tart"
    )
    modified = tmp_path / "contract.yaml"
    modified.write_text(yaml.safe_dump(contract))
    monkeypatch.setattr(walk, "_CONTRACT", modified)
    req = request(vm_name=None)
    assert req.cpu is req.memory_gb is req.image is req.tart_path is None
    resolved = walk.resolve_request(req)
    assert resolved.cpu == 6
    assert resolved.memory_gb == 12
    assert resolved.image == "stock:test"
    assert resolved.tart_path == "/opt/tart"
    assert resolved.vm_name.startswith("onex-walk-")
    assert len(resolved.vm_name) == len("onex-walk-") + 8


def test_transport_exception_is_receipted_and_delete_still_attempted() -> None:
    class FailingRunner(StubTartRunner):
        def run(
            self, argv: Sequence[str], *, timeout_seconds: float
        ) -> ModelRunnerResult:
            if (
                "--version" not in argv
                and self.kind(argv) == EnumWalkStepKind.TART_STOP
            ):
                self.calls.append(tuple(argv))
                raise OSError("SSH connection lost")
            return super().run(argv, timeout_seconds=timeout_seconds)

    receipt = walk.HandlerOnboardingCleanVmWalk(FailingRunner()).handle(request())
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert "SSH connection lost" in receipt.failure_text
    assert receipt.vm_deleted
    assert receipt.steps[-2].exit_code != 0
    assert receipt.steps[-1].kind == EnumWalkStepKind.TART_DELETE


def test_delete_failure_is_recorded() -> None:
    runner = StubTartRunner()
    runner.scripted[EnumWalkStepKind.TART_DELETE] = [
        ModelRunnerResult(exit_code=1, stdout="", stderr="delete refused")
    ]
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request())
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert not receipt.vm_deleted
    assert "delete refused" in receipt.failure_text


def test_no_successful_delegate_skips_dashboard_even_with_zero_script_exit() -> None:
    runner = StubTartRunner(
        SUCCESS_LOG.replace('"status":"success"', '"status":"failed"')
    )
    receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request())
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert receipt.delegated_run.status == "failed"
    assert receipt.dashboard.failure_text.startswith("not attempted:")
    assert receipt.dashboard.root_http_status is None
    assert EnumWalkStepKind.GUEST_DASHBOARD_START not in [s.kind for s in receipt.steps]
    assert receipt.vm_deleted


def test_readiness_retry_is_bounded_and_does_not_retry_other_errors() -> None:
    for message, expected_attempts in [
        ("agent unavailable", 20),
        ("curl: download failed", 1),
    ]:
        runner = StubTartRunner()
        runner.scripted[EnumWalkStepKind.GUEST_FETCH_SCRIPT] = [
            ModelRunnerResult(exit_code=1, stdout="", stderr=message)
        ] * 20
        with patch.object(walk.time, "sleep") as sleep:
            receipt = walk.HandlerOnboardingCleanVmWalk(runner).handle(request())
        assert receipt.outcome == EnumWalkOutcome.FAILED
        assert [s.kind for s in receipt.steps].count(
            EnumWalkStepKind.GUEST_FETCH_SCRIPT
        ) == expected_attempts
        assert sleep.call_count == expected_attempts - 1
        assert receipt.vm_deleted


def test_first_successful_delegate_and_backend_priority() -> None:
    payload = {
        "skill_name": "node_delegate_skill_orchestrator",
        "status": "success",
        "run_id": "first",
        "result": {
            "terminal_payload": {
                "backend": "local",
                "provider": "ollama",
                "route": "openai",
            }
        },
    }
    log = json.dumps(payload, separators=(",", ":")) + "\n" + SUCCESS_LOG
    assert parse_delegated_run(log).backend == "local"
    assert parse_delegated_run(log).run_id == "first"
    assert parse_delegated_run("no delegation").status == "not_run"
    assert (
        parse_delegated_run(
            '{"skill_name":"node_delegate_skill_orchestrator",invalid'
        ).status
        == "not_run"
    )


def test_cli_prints_receipt(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = StubTartRunner()
    monkeypatch.setattr(runtime, "HandlerSshTart", lambda _host: runner)
    monkeypatch.setattr("sys.argv", ["walk", "--input", request().model_dump_json()])
    assert runtime.main() == 0
    receipt = ModelWalkReceipt.model_validate_json(capsys.readouterr().out)
    assert receipt.outcome == EnumWalkOutcome.PASSED
    assert receipt.vm_deleted
