# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read-only principal: the ssh gate, the three-channel probe and the key install (step 0).

Every refusal test has a named injected break beside it: the same call against an overlay or a
executor with the defect planted must read the other way, which shows the refusal test can fail.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.lab_host_key import EnumKeyFile, EnumLineState
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_lab_host_key_distribution_effect.protocols.protocol_host_key_transport import (
    HostTransportError,
)
from omnimarket.nodes.node_read_only_principal_effect.handlers.adapter_memory_attempt_executor import (
    MemoryAttemptExecutor,
)
from omnimarket.nodes.node_read_only_principal_effect.handlers.adapter_subprocess_attempt_executor import (
    SubprocessAttemptExecutor,
)
from omnimarket.nodes.node_read_only_principal_effect.handlers.handler_read_only_command_gate import (
    HandlerReadOnlyCommandGate,
    decide,
    load_overlay,
)
from omnimarket.nodes.node_read_only_principal_effect.handlers.handler_read_only_principal_key_install import (
    HandlerReadOnlyPrincipalKeyInstall,
    authorized_line,
)
from omnimarket.nodes.node_read_only_principal_effect.handlers.handler_read_only_principal_probe import (
    HandlerReadOnlyPrincipalProbe,
)
from omnimarket.nodes.node_read_only_principal_effect.models import (
    EnumAttemptOutcome,
    EnumChannel,
    EnumGateDecision,
    EnumGateReason,
    EnumKeyInstallStatus,
    ModelAllowedCommand,
    ModelChannelProbe,
    ModelGateDecideRequest,
    ModelKeyInstallRequest,
    ModelProbeAttempt,
    ModelProbeRequest,
    ModelReadOnlyPrincipalOverlay,
    ModelSshGate,
)
from omnimarket.nodes.node_read_only_principal_effect.node import (
    NodeReadOnlyPrincipalEffect,
)
from omnimarket.nodes.node_read_only_principal_effect.protocols.protocol_attempt_executor import (
    ModelAttemptRun,
)
from omnimarket.nodes.node_read_only_principal_effect.runtime_read_only_principal import (
    EXIT_GATE_REFUSED,
    EXIT_NOT_CONFIGURED,
    parse_args,
    run_gate,
)

pytestmark = pytest.mark.unit

_BLOB = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"k" * 36).decode()
PUBLIC_KEY = f"ssh-ed25519 {_BLOB} generated"
GATE_COMMAND = "/usr/local/bin/onex-read-only-principal gate"

ALLOWED = (
    ModelAllowedCommand(argv_prefix=("uptime",)),
    ModelAllowedCommand(
        argv_prefix=("df",),
        allowed_flags=("-h",),
        max_operands=1,
        operand_pattern=r"/[A-Za-z0-9_./-]+",
    ),
    ModelAllowedCommand(
        argv_prefix=("cat",), max_operands=1, operand_pattern=r"/[A-Za-z0-9_./-]+"
    ),
    ModelAllowedCommand(
        argv_prefix=("docker", "ps"), allowed_flags=("-a", "--no-trunc")
    ),
)


def _gate(
    allowed: tuple[ModelAllowedCommand, ...] = ALLOWED,
    refused: tuple[str, ...] = (r"(^|/)\.\.(/|$)", r"\.env$"),
) -> ModelSshGate:
    return ModelSshGate(
        allowed_commands=allowed,
        refused_operand_patterns=refused,
        gate_command=GATE_COMMAND,
        key_path="/keys/read_only_principal",
        key_comment="read-only-principal",
    )


def _probe(channel: EnumChannel, stem: str) -> ModelChannelProbe:
    return ModelChannelProbe(
        channel=channel,
        read=ModelProbeAttempt(argv=(stem, "read")),
        mutate=ModelProbeAttempt(argv=(stem, "mutate")),
        refusal_markers=("permission denied", "not authorized"),
    )


def _overlay(
    allowed: tuple[ModelAllowedCommand, ...] = ALLOWED,
) -> ModelReadOnlyPrincipalOverlay:
    return ModelReadOnlyPrincipalOverlay(
        principal="ro-test",
        ssh=_gate(allowed),
        probes=(
            _probe(EnumChannel.SSH, "ssh-probe"),
            _probe(EnumChannel.GITHUB, "github-probe"),
            _probe(EnumChannel.LEDGER, "ledger-probe"),
        ),
    )


REFUSED_TEXT = ModelAttemptRun(exit_code=1, text="HTTP 403: Permission denied")
READ_OK = ModelAttemptRun(exit_code=0, text="fine")


def _answers(
    **overrides: ModelAttemptRun,
) -> dict[tuple[str, ...], ModelAttemptRun]:
    answers: dict[tuple[str, ...], ModelAttemptRun] = {}
    for stem in ("ssh-probe", "github-probe", "ledger-probe"):
        answers[(stem, "read")] = READ_OK
        answers[(stem, "mutate")] = REFUSED_TEXT
    for key, value in overrides.items():
        stem, kind = key.split("__")
        answers[(stem.replace("_", "-"), kind)] = value
    return answers


# ---------------------------------------------------------------- the ssh gate


@pytest.mark.parametrize(
    "command",
    [
        "uptime",
        "df -h",
        "df -h /data",
        "cat /var/log/example.log",
        "docker ps",
        "docker ps -a --no-trunc",
        "  uptime  ",
    ],
)
def test_golden_a_read_command_on_the_allowlist_is_allowed(command: str) -> None:
    got = decide(command, _gate())
    assert got.decision == EnumGateDecision.ALLOW
    assert got.reason == EnumGateReason.ALLOWED
    assert got.argv == tuple(command.split())


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("", EnumGateReason.EMPTY),
        ("   ", EnumGateReason.EMPTY),
        ("rm -rf /data", EnumGateReason.NOT_ON_ALLOWLIST),
        ("docker rm web", EnumGateReason.NOT_ON_ALLOWLIST),
        ("docker exec web sh", EnumGateReason.NOT_ON_ALLOWLIST),
        ("systemctl restart sshd", EnumGateReason.NOT_ON_ALLOWLIST),
        ("df -h; rm -rf /", EnumGateReason.FLAG_NOT_ALLOWED),
        ("df --output=target", EnumGateReason.FLAG_NOT_ALLOWED),
        ("docker ps --format x", EnumGateReason.FLAG_NOT_ALLOWED),
        ("cat /a /b", EnumGateReason.TOO_MANY_OPERANDS),
        ("uptime now", EnumGateReason.TOO_MANY_OPERANDS),
        ("cat /a;rm", EnumGateReason.OPERAND_SHAPE),
        ("cat /a>/b", EnumGateReason.OPERAND_SHAPE),
        ("cat $(rm x)", EnumGateReason.OPERAND_SHAPE),
        ("cat relative.txt", EnumGateReason.OPERAND_SHAPE),
        ("cat /srv/../etc/passwd", EnumGateReason.OPERAND_REFUSED),
        ("cat /srv/app/.env", EnumGateReason.OPERAND_REFUSED),
        ("cat 'unclosed", EnumGateReason.UNPARSEABLE),
        ("uptime\nrm -rf /", EnumGateReason.UNPARSEABLE),
        ("uptime\x00rm", EnumGateReason.UNPARSEABLE),
    ],
)
def test_error_a_mutating_or_malformed_command_is_refused_with_its_rule(
    command: str, reason: EnumGateReason
) -> None:
    got = decide(command, _gate())
    assert got.decision == EnumGateDecision.REFUSE
    assert got.reason == reason
    assert got.argv == ()


def test_the_refusal_names_the_rule_and_never_the_command() -> None:
    got = decide("cat /secret-token-value;x", _gate())
    assert "secret-token-value" not in got.model_dump_json()


def test_break_a_planted_rm_entry_turns_the_refusal_test_red() -> None:
    """The control: the same mutating call, against an allowlist with ``rm`` planted, is allowed."""
    planted = _gate(
        (
            *ALLOWED,
            ModelAllowedCommand(
                argv_prefix=("rm",),
                allowed_flags=("-rf",),
                max_operands=1,
                operand_pattern=r"/.*",
            ),
        )
    )
    assert decide("rm -rf /data", _gate()).decision == EnumGateDecision.REFUSE
    assert decide("rm -rf /data", planted).decision == EnumGateDecision.ALLOW


def test_break_a_dropped_refused_pattern_turns_the_traversal_refusal_red() -> None:
    assert decide("cat /srv/../etc/x", _gate()).decision == EnumGateDecision.REFUSE
    broken = _gate(refused=())
    assert decide("cat /srv/../etc/x", broken).decision == EnumGateDecision.ALLOW


def test_the_gate_handler_decides_from_an_explicit_overlay_without_reading_a_file() -> (
    None
):
    def no_file() -> ModelReadOnlyPrincipalOverlay:
        raise AssertionError("an explicit overlay must not read the overlay file")

    handler = HandlerReadOnlyCommandGate(overlay_source=no_file)
    got = handler.handle(ModelGateDecideRequest(command="uptime", overlay=_overlay()))
    assert got.decision == EnumGateDecision.ALLOW


def test_run_gate_runs_an_allowed_request_as_argv_with_no_shell() -> None:
    ran: list[tuple[str, ...]] = []
    handler = HandlerReadOnlyCommandGate(overlay_source=_overlay)
    code = run_gate(
        {"SSH_ORIGINAL_COMMAND": "docker ps -a"},
        gate=handler,
        run=lambda argv: ran.append(argv) or 0,
    )
    assert code == 0
    assert ran == [("docker", "ps", "-a")]


def test_run_gate_refuses_a_mutation_and_starts_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[tuple[str, ...]] = []
    handler = HandlerReadOnlyCommandGate(overlay_source=_overlay)
    code = run_gate(
        {"SSH_ORIGINAL_COMMAND": "docker rm web"},
        gate=handler,
        run=lambda argv: ran.append(argv) or 0,
    )
    assert code == EXIT_GATE_REFUSED
    assert ran == []
    assert "not-on-allowlist" in capsys.readouterr().err


def test_run_gate_with_no_request_is_refused_not_a_login_shell() -> None:
    ran: list[tuple[str, ...]] = []
    handler = HandlerReadOnlyCommandGate(overlay_source=_overlay)
    assert (
        run_gate({}, gate=handler, run=lambda argv: ran.append(argv) or 0)
        == EXIT_GATE_REFUSED
    )
    assert ran == []


def test_run_gate_fails_closed_when_the_overlay_is_not_configured() -> None:
    ran: list[tuple[str, ...]] = []

    def missing() -> ModelReadOnlyPrincipalOverlay:
        raise NodeOverlayError("overlay not configured")

    code = run_gate(
        {"SSH_ORIGINAL_COMMAND": "uptime"},
        gate=HandlerReadOnlyCommandGate(overlay_source=missing),
        run=lambda argv: ran.append(argv) or 0,
    )
    assert code == EXIT_NOT_CONFIGURED
    assert ran == []


def test_load_overlay_without_a_source_names_the_setting(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError, match="node_read_only_principal_effect"):
        load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_the_overlay_file_loads_and_a_broken_one_is_refused(tmp_path: Path) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(_overlay().model_dump_json(), encoding="utf-8")
    env = {"ONEX_READ_ONLY_PRINCIPAL_OVERLAY_FILE": str(good)}
    assert load_overlay(env).principal == "ro-test"
    bad = tmp_path / "bad.yaml"
    bad.write_text("principal: x\n", encoding="utf-8")
    with pytest.raises(NodeOverlayError):
        load_overlay({"ONEX_READ_ONLY_PRINCIPAL_OVERLAY_FILE": str(bad)})


# ------------------------------------------------------------- the three-channel probe


def test_golden_every_channel_reads_and_refuses_its_mutation() -> None:
    executor = MemoryAttemptExecutor(_answers())
    result = HandlerReadOnlyPrincipalProbe(executor=executor).handle(
        ModelProbeRequest(overlay=_overlay())
    )
    assert result.passed
    assert {r.channel for r in result.receipts} == set(EnumChannel)
    assert all(r.read == EnumAttemptOutcome.READ_OK for r in result.receipts)
    assert all(r.mutate == EnumAttemptOutcome.MUTATION_REFUSED for r in result.receipts)
    assert len(executor.calls) == 6
    assert result.complete
    assert result.lines()[-1] == "principal=ro-test verdict=PASS"


@pytest.mark.parametrize("channel", ["ssh", "github", "ledger"])
def test_error_a_mutation_the_credential_accepts_fails_the_verdict(
    channel: str,
) -> None:
    accepted = ModelAttemptRun(exit_code=0, text="created")
    result = HandlerReadOnlyPrincipalProbe(
        executor=MemoryAttemptExecutor(
            _answers(**{f"{channel}_probe__mutate": accepted})
        )
    ).handle(ModelProbeRequest(overlay=_overlay()))
    assert not result.passed
    hit = next(r for r in result.receipts if r.channel.value == channel)
    assert hit.mutate == EnumAttemptOutcome.MUTATION_ACCEPTED
    assert result.lines()[-1] == "principal=ro-test verdict=FAIL"


def test_error_a_mutation_that_fails_without_a_refusal_word_is_not_a_refusal() -> None:
    dead_network = ModelAttemptRun(exit_code=1, text="connection reset by peer")
    result = HandlerReadOnlyPrincipalProbe(
        executor=MemoryAttemptExecutor(_answers(github_probe__mutate=dead_network))
    ).handle(ModelProbeRequest(overlay=_overlay()))
    assert not result.passed
    hit = next(r for r in result.receipts if r.channel == EnumChannel.GITHUB)
    assert hit.mutate == EnumAttemptOutcome.MUTATION_INDETERMINATE


def test_error_a_channel_that_cannot_read_fails_even_when_its_mutation_is_refused() -> (
    None
):
    result = HandlerReadOnlyPrincipalProbe(
        executor=MemoryAttemptExecutor(
            _answers(ledger_probe__read=ModelAttemptRun(exit_code=1, text="down"))
        )
    ).handle(ModelProbeRequest(overlay=_overlay()))
    assert not result.passed
    hit = next(r for r in result.receipts if r.channel == EnumChannel.LEDGER)
    assert hit.read == EnumAttemptOutcome.READ_FAILED
    assert hit.mutate == EnumAttemptOutcome.MUTATION_REFUSED


def test_error_a_channel_whose_attempts_are_not_scripted_never_reads_as_a_pass() -> (
    None
):
    result = HandlerReadOnlyPrincipalProbe(executor=MemoryAttemptExecutor({})).handle(
        ModelProbeRequest(overlay=_overlay())
    )
    assert not result.passed


def test_the_mutation_is_attempted_even_when_the_read_failed() -> None:
    executor = MemoryAttemptExecutor(
        _answers(ssh_probe__read=ModelAttemptRun(exit_code=255, text=""))
    )
    HandlerReadOnlyPrincipalProbe(executor=executor).handle(
        ModelProbeRequest(overlay=_overlay())
    )
    assert ("ssh-probe", "mutate") in executor.calls


def test_the_receipt_holds_outcomes_and_never_output_text() -> None:
    leaky = ModelAttemptRun(exit_code=1, text="Permission denied token=SECRETVALUE123")
    result = HandlerReadOnlyPrincipalProbe(
        executor=MemoryAttemptExecutor(_answers(github_probe__mutate=leaky))
    ).handle(ModelProbeRequest(overlay=_overlay()))
    assert result.passed
    assert "SECRETVALUE123" not in result.model_dump_json()
    assert "SECRETVALUE123" not in "\n".join(result.lines())


def test_break_a_executor_that_accepts_every_mutation_turns_the_golden_probe_red() -> (
    None
):
    """The control for the golden test: a credential that is not read-only must read FAIL."""
    answers = {
        key: (READ_OK if key[1] == "read" else ModelAttemptRun(exit_code=0, text="ok"))
        for key in _answers()
    }
    result = HandlerReadOnlyPrincipalProbe(
        executor=MemoryAttemptExecutor(answers)
    ).handle(ModelProbeRequest(overlay=_overlay()))
    assert not result.passed
    assert all(
        r.mutate == EnumAttemptOutcome.MUTATION_ACCEPTED for r in result.receipts
    )


def test_golden_a_narrowed_probe_runs_only_its_channel_and_reads_pass_partial() -> None:
    executor = MemoryAttemptExecutor(_answers())
    result = HandlerReadOnlyPrincipalProbe(executor=executor).handle(
        ModelProbeRequest(overlay=_overlay(), channels=(EnumChannel.SSH,))
    )
    assert result.passed
    assert not result.complete
    assert [r.channel for r in result.receipts] == [EnumChannel.SSH]
    assert executor.calls == [("ssh-probe", "read"), ("ssh-probe", "mutate")]
    assert result.lines()[-1] == "principal=ro-test verdict=PASS-PARTIAL"


def test_error_a_narrowed_probe_whose_channel_accepts_a_mutation_reads_fail() -> None:
    accepted = ModelAttemptRun(exit_code=0, text="created")
    result = HandlerReadOnlyPrincipalProbe(
        executor=MemoryAttemptExecutor(_answers(ssh_probe__mutate=accepted))
    ).handle(ModelProbeRequest(overlay=_overlay(), channels=(EnumChannel.SSH,)))
    assert result.verdict == "FAIL"


def test_a_channel_named_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match="named once"):
        ModelProbeRequest(channels=(EnumChannel.SSH, EnumChannel.SSH))


def test_break_a_probe_that_ignores_the_narrowing_would_read_a_full_pass() -> None:
    """The control for PASS-PARTIAL: the partial verdict is not a full PASS, whatever passed."""
    partial = HandlerReadOnlyPrincipalProbe(
        executor=MemoryAttemptExecutor(_answers())
    ).handle(ModelProbeRequest(overlay=_overlay(), channels=(EnumChannel.SSH,)))
    full = HandlerReadOnlyPrincipalProbe(
        executor=MemoryAttemptExecutor(_answers())
    ).handle(ModelProbeRequest(overlay=_overlay()))
    assert partial.verdict != full.verdict


def test_the_node_class_probes_through_the_same_handler() -> None:
    node = NodeReadOnlyPrincipalEffect(executor=MemoryAttemptExecutor(_answers()))
    assert node.handle(ModelProbeRequest(overlay=_overlay())).passed
    assert node.contract_path.is_file()


def test_the_overlay_must_probe_each_channel_exactly_once() -> None:
    base = _overlay()
    with pytest.raises(ValidationError, match="exactly once"):
        ModelReadOnlyPrincipalOverlay.model_validate(
            {**base.model_dump(), "probes": base.model_dump()["probes"][:2]}
        )
    with pytest.raises(ValidationError, match="exactly once"):
        ModelReadOnlyPrincipalOverlay.model_validate(
            {
                **base.model_dump(),
                "probes": [
                    *base.model_dump()["probes"],
                    base.model_dump()["probes"][0],
                ],
            }
        )


def test_the_subprocess_executor_passes_only_the_named_variables() -> None:
    environ = {"PATH": "/usr/bin:/bin", "NAMED": "yes", "UNNAMED": "leak"}
    executor = SubprocessAttemptExecutor(environ)
    code = (
        "import os,sys;"
        "sys.exit(0 if os.environ.get('NAMED')=='yes' and 'UNNAMED' not in os.environ else 3)"
    )
    run = executor.run(
        ModelProbeAttempt(argv=(sys.executable, "-c", code), pass_env=("NAMED",))
    )
    assert run.exit_code == 0


def test_the_subprocess_executor_reports_a_missing_program_and_a_timeout() -> None:
    executor = SubprocessAttemptExecutor({"PATH": "/usr/bin:/bin"})
    missing = executor.run(ModelProbeAttempt(argv=("no-such-program-for-the-probe",)))
    assert missing.exit_code == 127
    slow = executor.run(
        ModelProbeAttempt(
            argv=(sys.executable, "-c", "import time;time.sleep(5)"), timeout_s=1
        )
    )
    assert slow.exit_code == 124


# ----------------------------------------------------------------- the key install


class FakeTransport:
    def __init__(self) -> None:
        self.files: dict[str, list[str]] = {}
        self.refuse: set[str] = set()
        self.applied: list[tuple[str, bool]] = []

    def read_material(self, host: object, request: object) -> object:
        raise AssertionError("the key install reads no host material")

    def ensure_line(
        self,
        host: ModelLabHostRow,
        file: EnumKeyFile,
        needle: str,
        line: str,
        *,
        apply: bool,
    ) -> EnumLineState:
        assert file == EnumKeyFile.AUTHORIZED_KEYS
        if host.name in self.refuse:
            raise HostTransportError("Permission denied (publickey)")
        held = self.files.setdefault(host.name, [])
        self.applied.append((host.name, apply))
        if any(needle in existing for existing in held):
            return EnumLineState.PRESENT
        if apply:
            held.append(line)
        return EnumLineState.ADDED


def _table() -> ModelLabHostsTable:
    return ModelLabHostsTable(
        hosts=(
            ModelLabHostRow(name="alpha", target="alpha.example.invalid"),
            ModelLabHostRow(name="beta", target="beta.example.invalid"),
            ModelLabHostRow(
                name="gamma",
                target="gamma.example.invalid",
                available=False,
                unavailable_reason="off the pool",
            ),
        )
    )


def _installer(
    tmp_path: Path, transport: FakeTransport, made: list[Path]
) -> tuple[HandlerReadOnlyPrincipalKeyInstall, ModelReadOnlyPrincipalOverlay]:
    key = tmp_path / "keys" / "read_only_principal"
    overlay = _overlay().model_copy(
        update={"ssh": _gate().model_copy(update={"key_path": str(key)})}
    )

    def keygen(path: Path, comment: str) -> None:
        made.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE-PLACEHOLDER", encoding="utf-8")
        Path(f"{path}.pub").write_text(f"{PUBLIC_KEY}\n", encoding="utf-8")

    handler = HandlerReadOnlyPrincipalKeyInstall(
        transport=transport,
        table_source=_table,
        overlay_source=lambda: overlay,
        keygen=keygen,
    )
    return handler, overlay


def test_golden_the_install_makes_the_key_and_forces_it_to_the_gate(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    made: list[Path] = []
    handler, overlay = _installer(tmp_path, transport, made)
    result = handler.handle(ModelKeyInstallRequest())
    assert result.exit_code == 0
    assert result.key_created
    assert len(made) == 1
    statuses = {h.host: h.status for h in result.hosts}
    assert statuses == {
        "alpha": EnumKeyInstallStatus.OK,
        "beta": EnumKeyInstallStatus.OK,
        "gamma": EnumKeyInstallStatus.SKIPPED,
    }
    line = transport.files["alpha"][0]
    assert line.startswith(f'restrict,command="{GATE_COMMAND}" ssh-ed25519 ')
    assert line.endswith(f" {overlay.ssh.key_comment}")
    assert "PRIVATE-PLACEHOLDER" not in "\n".join(result.lines())
    assert "gamma" not in transport.files


def test_the_second_install_changes_nothing(tmp_path: Path) -> None:
    transport = FakeTransport()
    made: list[Path] = []
    handler, _ = _installer(tmp_path, transport, made)
    handler.handle(ModelKeyInstallRequest())
    before = {k: list(v) for k, v in transport.files.items()}
    again = handler.handle(ModelKeyInstallRequest())
    assert again.exit_code == 0
    assert not again.key_created
    assert len(made) == 1
    assert transport.files == before
    assert {h.detail for h in again.hosts if h.host != "gamma"} == {"already-present"}


def test_dry_run_writes_no_key_and_no_line(tmp_path: Path) -> None:
    transport = FakeTransport()
    made: list[Path] = []
    handler, _ = _installer(tmp_path, transport, made)
    result = handler.handle(ModelKeyInstallRequest(dry_run=True))
    assert made == []
    assert transport.files == {}
    assert result.key_created
    assert result.dry_run
    assert result.lines()[0] == "key would-create"


def test_dry_run_with_a_key_present_plans_each_host_and_writes_nothing(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    made: list[Path] = []
    handler, _ = _installer(tmp_path, transport, made)
    handler.handle(ModelKeyInstallRequest(hosts=("alpha",)))
    transport.files.clear()
    result = handler.handle(ModelKeyInstallRequest(dry_run=True))
    assert {h.host: h.detail for h in result.hosts if h.host == "beta"} == {
        "beta": "would-add"
    }
    assert transport.files.get("beta", []) == []


def test_error_a_host_that_refuses_fails_the_install_and_the_others_still_take_it(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    transport.refuse.add("alpha")
    handler, _ = _installer(tmp_path, transport, [])
    result = handler.handle(ModelKeyInstallRequest())
    assert result.exit_code == 1
    assert {h.host: h.status for h in result.hosts}[
        "alpha"
    ] == EnumKeyInstallStatus.REFUSED
    assert len(transport.files["beta"]) == 1


def test_error_an_unknown_host_is_refused_before_any_key_is_made(
    tmp_path: Path,
) -> None:
    made: list[Path] = []
    handler, _ = _installer(tmp_path, FakeTransport(), made)
    result = handler.handle(ModelKeyInstallRequest(hosts=("nowhere",)))
    assert result.exit_code == 2
    assert result.refusal is not None
    assert made == []


def test_error_a_key_the_tool_cannot_make_is_refused_and_installs_nothing(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    overlay = _overlay().model_copy(
        update={"ssh": _gate().model_copy(update={"key_path": str(tmp_path / "k")})}
    )

    def broken(path: Path, comment: str) -> None:
        raise OSError("ssh-keygen did not make the key")

    handler = HandlerReadOnlyPrincipalKeyInstall(
        transport=transport,
        table_source=_table,
        overlay_source=lambda: overlay,
        keygen=broken,
    )
    result = handler.handle(ModelKeyInstallRequest())
    assert result.exit_code == 2
    assert transport.files == {}


def test_break_a_line_without_restrict_would_be_a_full_login() -> None:
    """The control for the golden install: the line text, not the transport, carries the force."""
    line = authorized_line(GATE_COMMAND, PUBLIC_KEY, "c")
    assert line.startswith("restrict,command=")
    assert not f"ssh-ed25519 {_BLOB} c".startswith("restrict")


def test_the_gate_command_cannot_hold_a_quote_that_would_end_the_force() -> None:
    with pytest.raises(ValidationError):
        ModelSshGate(
            allowed_commands=ALLOWED,
            gate_command='x" ssh-ed25519 AAAA',
            key_path="/k",
            key_comment="c",
        )


def test_the_command_line_parses() -> None:
    assert parse_args(["probe"]).command == "probe"
    assert parse_args(["install-key", "--dry-run", "--host", "alpha"]).host == ["alpha"]
    assert parse_args(["gate"]).command == "gate"
    assert parse_args(["probe", "--channel", "ssh"]).channel == ["ssh"]


def test_the_contract_names_the_three_operations() -> None:
    text = NodeReadOnlyPrincipalEffect.contract_path.read_text(encoding="utf-8")
    for operation in ("probe", "gate_decide", "install_key"):
        assert f"operation: {operation}" in text
