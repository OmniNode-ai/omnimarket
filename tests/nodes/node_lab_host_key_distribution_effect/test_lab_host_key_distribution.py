# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public key distribution, isolation and transport behavior."""

from __future__ import annotations

import base64
import re
import stat
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_lab_host_key_distribution_effect.handlers import (
    adapter_ssh_host_key_transport as adapter,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.handlers.handler_lab_host_key_distribution import (
    HandlerLabHostKeyDistribution,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.models import (
    EnumKeyFile,
    EnumLineState,
    EnumPairStatus,
    ModelHostKeyMaterial,
    ModelLabHostKeyDistributionRequest,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.node import (
    NodeLabHostKeyDistributionEffect,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.protocols import (
    HostTransportError,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.runtime_lab_host_key_distribution import (
    main,
)

pytestmark = pytest.mark.unit


def _key(name: str, kind: str) -> str:
    encoded = base64.b64encode(f"{name}-{kind}".encode()).decode()
    return f"ssh-ed25519 {encoded} {name}-{kind}"


class FakeTransport:
    def __init__(self, table: ModelLabHostsTable) -> None:
        self.files: dict[str, dict[str, list[str]]] = {
            row.name: {"authorized_keys": [], "known_hosts": []} for row in table.hosts
        }
        self.calls: list[str] = []
        self.reads: Counter[str] = Counter()
        self.appends: list[tuple[str, EnumKeyFile, str]] = []
        self.unreachable: set[str] = set()
        self.fail_ensure: tuple[str, EnumKeyFile] | None = None

    def read_material(
        self, host: ModelLabHostRow, request: ModelLabHostKeyDistributionRequest
    ) -> ModelHostKeyMaterial:
        self.calls.append(host.name)
        self.reads[host.name] += 1
        if host.name in self.unreachable:
            raise HostTransportError("Permission denied (publickey)")
        return ModelHostKeyMaterial(
            user_key=_key(host.name, "user"), host_keys=(_key(host.name, "host"),)
        )

    def ensure_line(
        self,
        host: ModelLabHostRow,
        file: EnumKeyFile,
        needle: str,
        line: str,
        *,
        apply: bool,
    ) -> EnumLineState:
        self.calls.append(host.name)
        if (host.name, file) == self.fail_ensure:
            raise HostTransportError("rc=1: file is not writable")
        lines = self.files[host.name][file.value]
        if any(needle in existing for existing in lines):
            return EnumLineState.PRESENT
        if not apply:
            return EnumLineState.ABSENT
        lines.append(line)
        self.appends.append((host.name, file, line))
        return EnumLineState.ADDED

    def snapshot(self) -> dict[str, dict[str, bytes]]:
        return {
            host: {file: "\n".join(lines).encode() for file, lines in files.items()}
            for host, files in self.files.items()
        }


@pytest.fixture
def table() -> ModelLabHostsTable:
    return ModelLabHostsTable(
        hosts=tuple(
            ModelLabHostRow(
                name=name,
                target=f"{name}.example.invalid",
                available=name != "delta",
            )
            for name in ("alpha", "beta", "gamma", "delta")
        )
    )


@pytest.fixture
def request_model(table: ModelLabHostsTable) -> ModelLabHostKeyDistributionRequest:
    return ModelLabHostKeyDistributionRequest(
        hosts=("alpha", "beta", "gamma"), table=table
    )


def test_lab_host_key_distribution_idempotent(
    table: ModelLabHostsTable, request_model: ModelLabHostKeyDistributionRequest
) -> None:
    fake = FakeTransport(table)
    handler = HandlerLabHostKeyDistribution(fake)
    first = handler.handle(request_model)
    assert first.exit_code == 0
    assert len(first.pairs) == 6
    assert all(pair.status == EnumPairStatus.OK for pair in first.pairs)
    assert all(
        pair.authorized_key_added and pair.known_hosts_added for pair in first.pairs
    )
    assert [(p.source, p.destination) for p in first.pairs] == [
        (source, dest)
        for source in request_model.hosts
        for dest in request_model.hosts
        if source != dest
    ]
    assert fake.reads == Counter(request_model.hosts)
    for source in request_model.hosts:
        for destination in request_model.hosts:
            if source == destination:
                continue
            assert _key(source, "user") in fake.files[destination]["authorized_keys"]
            host_key = " ".join(_key(destination, "host").split()[:2])
            expected = f"{destination},{destination}.example.invalid {host_key}"
            assert expected in fake.files[source]["known_hosts"]
    assert "delta" not in fake.calls
    assert all(
        b"delta" not in value
        for files in fake.snapshot().values()
        for value in files.values()
    )
    snapshot = fake.snapshot()
    fake.appends.clear()
    second = handler.handle(request_model)
    assert second.exit_code == 0
    assert all(
        not p.authorized_key_added and not p.known_hosts_added for p in second.pairs
    )
    assert fake.appends == []
    assert fake.snapshot() == snapshot
    assert all(
        "authorized_keys=present known_hosts=present" in line for line in second.lines()
    )


def test_lab_host_key_distribution_dry_run_writes_nothing(
    table: ModelLabHostsTable, request_model: ModelLabHostKeyDistributionRequest
) -> None:
    fake = FakeTransport(table)
    before = fake.snapshot()
    result = HandlerLabHostKeyDistribution(fake).handle(
        request_model.model_copy(update={"dry_run": True})
    )
    assert result.exit_code == 0
    assert len(result.pairs) == 6
    assert all(p.status == EnumPairStatus.PLANNED for p in result.pairs)
    assert all(p.authorized_key_added and p.known_hosts_added for p in result.pairs)
    assert fake.snapshot() == before
    assert fake.appends == []
    assert all(
        "authorized_keys=would-add known_hosts=would-add" in line
        for line in result.lines()
    )


def test_lab_host_key_distribution_unreachable_host_is_a_refusal_with_text(
    table: ModelLabHostsTable, request_model: ModelLabHostKeyDistributionRequest
) -> None:
    fake = FakeTransport(table)
    fake.unreachable.add("beta")
    result = HandlerLabHostKeyDistribution(fake).handle(request_model)
    assert result.exit_code == 1
    for pair in result.pairs:
        if "beta" in (pair.source, pair.destination):
            assert pair.status == EnumPairStatus.REFUSED
            assert "Permission denied (publickey)" in pair.detail
        else:
            assert pair.status == EnumPairStatus.OK
    assert sum(p.status == EnumPairStatus.REFUSED for p in result.pairs) == 4
    assert all(
        "Permission denied (publickey)" in line
        for line in result.lines()
        if "refused" in line
    )


@pytest.mark.parametrize("name", ["absent", "delta"])
def test_lab_host_key_distribution_unknown_or_unavailable_scope_refused(
    name: str, table: ModelLabHostsTable
) -> None:
    fake = FakeTransport(table)
    result = HandlerLabHostKeyDistribution(fake).handle(
        ModelLabHostKeyDistributionRequest(hosts=("alpha", name), table=table)
    )
    assert result.exit_code == 2
    assert result.refusal is not None
    assert name in result.refusal
    assert result.pairs == ()
    assert fake.calls == []


def test_lab_host_key_distribution_handler_holds_no_host_literal() -> None:
    node = NodeLabHostKeyDistributionEffect.contract_path.parent
    private_ipv4 = re.compile(
        r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
    )
    for folder in ("handlers", "models", "protocols"):
        for path in (node / folder).glob("*.py"):
            text = path.read_text()
            assert private_ipv4.search(text) is None, path
            assert re.search(r"\bh\d{3}\b", text) is None, path


def test_lab_host_key_distribution_adapter_builds_ssh_argv_and_passes_line_on_stdin(
    monkeypatch: pytest.MonkeyPatch, table: ModelLabHostsTable
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    material = f"__USER_KEY__\n{_key('alpha', 'user')}\n__HOST_KEYS__\n{_key('alpha', 'host')}\n__END__\n"

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        output = material if "__USER_KEY__" in argv[-1] else "added\n"
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    monkeypatch.setattr(adapter.subprocess, "run", run)
    transport = adapter.SshHostKeyTransport()
    row = table.hosts[0]
    request = ModelLabHostKeyDistributionRequest(hosts=(row.name,), timeout_s=19)
    transport.read_material(row, request)
    line = _key("beta", "user")
    assert (
        transport.ensure_line(
            row,
            EnumKeyFile.AUTHORIZED_KEYS,
            " ".join(line.split()[:2]),
            line,
            apply=True,
        )
        == EnumLineState.ADDED
    )
    argv, kwargs = calls[-1]
    assert argv[:6] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        row.target,
    ]
    assert argv[-1].startswith("bash -c ")
    assert all(line not in arg for arg in argv)
    assert line in str(kwargs["input"])
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 19
    transport.read_material(row, request.model_copy(update={"local_host": row.name}))
    transport.ensure_line(row, EnumKeyFile.AUTHORIZED_KEYS, line, line, apply=False)
    assert calls[-1][0][:2] == ["bash", "-c"]
    transport.read_material(row.model_copy(update={"local": True}), request)
    assert calls[-1][0][:2] == ["bash", "-c"]


def test_lab_host_key_distribution_never_reads_a_private_key(
    monkeypatch: pytest.MonkeyPatch, table: ModelLabHostsTable
) -> None:
    calls: list[tuple[list[str], object]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs["input"]))
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="missing public keys"
        )

    monkeypatch.setattr(adapter.subprocess, "run", run)
    request = ModelLabHostKeyDistributionRequest(hosts=("alpha",))
    with pytest.raises(HostTransportError, match="rc=1: missing public keys"):
        adapter.SshHostKeyTransport().read_material(table.hosts[0], request)
    payload = str(calls[0][1]).splitlines()
    assert payload == [
        "3",
        *request.identity_public_files,
        "1",
        *request.host_key_files,
    ]
    assert all(path.endswith(".pub") for path in payload if not path.isdigit())
    assert 'cat -- "$HOME/.ssh/$name"' in calls[0][0][-1]
    assert 'cat -- "$path"' in calls[0][0][-1]
    for field, value in [
        ("identity_public_files", ("id_ed25519",)),
        ("identity_public_files", ("../id_ed25519.pub",)),
        ("host_key_files", ("/etc/ssh/ssh_host_ed25519_key",)),
        ("host_key_files", ("relative.pub",)),
    ]:
        with pytest.raises(ValidationError):
            ModelLabHostKeyDistributionRequest.model_validate(
                {"hosts": ["alpha"], field: value}
            )


def test_local_adapter_reads_public_files_and_appends_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    private = ssh / "id_ed25519"
    private.write_text("private sentinel never returned")
    (ssh / "id_ecdsa.pub").write_text(_key("alpha", "user") + "\n")
    host_key = tmp_path / "host.pub"
    host_key.write_text(_key("alpha", "host") + "\n")
    row = ModelLabHostRow(name="alpha", target="alpha.example.invalid", local=True)
    request = ModelLabHostKeyDistributionRequest(
        hosts=(row.name,), host_key_files=(str(host_key),)
    )
    transport = adapter.SshHostKeyTransport()
    material = transport.read_material(row, request)
    assert material.user_key == _key("alpha", "user")
    assert material.host_keys == (_key("alpha", "host"),)
    line = _key("beta", "user")
    needle = " ".join(line.split()[:2])
    assert (
        transport.ensure_line(
            row, EnumKeyFile.AUTHORIZED_KEYS, needle, line, apply=False
        )
        == EnumLineState.ABSENT
    )
    assert not (ssh / "authorized_keys").exists()
    assert (
        transport.ensure_line(
            row, EnumKeyFile.AUTHORIZED_KEYS, needle, line, apply=True
        )
        == EnumLineState.ADDED
    )
    file = ssh / "authorized_keys"
    before = file.read_bytes()
    assert (
        transport.ensure_line(
            row, EnumKeyFile.AUTHORIZED_KEYS, needle, line, apply=True
        )
        == EnumLineState.PRESENT
    )
    assert file.read_bytes() == before == (line + "\n").encode()
    assert stat.S_IMODE(ssh.stat().st_mode) == 0o700
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert private.read_text() == "private sentinel never returned"
    host_key.unlink()
    with pytest.raises(HostTransportError, match="rc=1: no readable public host key"):
        transport.read_material(row, request)
    (ssh / "id_ecdsa.pub").unlink()
    with pytest.raises(HostTransportError, match="rc=1: no public identity key"):
        transport.read_material(row, request)


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_adapter_retains_stderr_and_exit_status(
    failure: str, monkeypatch: pytest.MonkeyPatch, table: ModelLabHostsTable
) -> None:
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 7, stderr=b"  connection stalled  ")
        return subprocess.CompletedProcess(
            argv, 255, stdout="", stderr=" Permission denied (publickey) " + "x" * 400
        )

    monkeypatch.setattr(adapter.subprocess, "run", run)
    with pytest.raises(HostTransportError) as exc:
        adapter.SshHostKeyTransport().read_material(
            table.hosts[0], ModelLabHostKeyDistributionRequest(hosts=("alpha",))
        )
    text = str(exc.value)
    if failure == "timeout":
        assert text == "rc=124 timeout: connection stalled"
    else:
        assert text.startswith("rc=255: Permission denied (publickey)")
        assert len(text.removeprefix("rc=255: ")) == 300


def test_adapter_rejects_unsafe_target_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*args: object, **kwargs: object) -> None:
        pytest.fail("unsafe target must not spawn a process")

    monkeypatch.setattr(adapter.subprocess, "run", run)
    row = ModelLabHostRow(name="alpha", target="-oProxyCommand=bad")
    with pytest.raises(HostTransportError, match="invalid SSH target"):
        adapter.SshHostKeyTransport().read_material(
            row, ModelLabHostKeyDistributionRequest(hosts=(row.name,))
        )


def test_ensure_failure_refuses_only_affected_pairs(
    table: ModelLabHostsTable, request_model: ModelLabHostKeyDistributionRequest
) -> None:
    fake = FakeTransport(table)
    fake.fail_ensure = ("beta", EnumKeyFile.KNOWN_HOSTS)
    result = HandlerLabHostKeyDistribution(fake).handle(request_model)
    assert result.exit_code == 1
    assert sum(p.status == EnumPairStatus.REFUSED for p in result.pairs) == 2
    assert all(
        p.detail == "rc=1: file is not writable"
        for p in result.pairs
        if p.status == EnumPairStatus.REFUSED
    )
    assert all(
        p.status == EnumPairStatus.OK for p in result.pairs if p.source != "beta"
    )


def test_table_resolution_and_cli_scope_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], table: ModelLabHostsTable
) -> None:
    path = tmp_path / "table.yaml"
    path.write_text(yaml.safe_dump(table.model_dump(mode="json")))
    fake = FakeTransport(table)
    handler = HandlerLabHostKeyDistribution(fake, table_source=lambda: table)
    assert (
        handler.handle(
            ModelLabHostKeyDistributionRequest(hosts=("alpha", "beta"))
        ).exit_code
        == 0
    )
    from_file = handler.handle(
        ModelLabHostKeyDistributionRequest(
            hosts=("alpha", "beta"), table_path=str(path)
        )
    )
    assert from_file.exit_code == 0
    assert len(from_file.pairs) == 2
    assert main(["--host", "delta", "--table", str(path), "--dry-run"]) == 2
    assert "delta" in capsys.readouterr().err
    path.write_text("hosts: invalid")
    result = handler.handle(
        ModelLabHostKeyDistributionRequest(hosts=("alpha",), table_path=str(path))
    )
    assert result.exit_code == 2
    assert result.refusal
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_known_host_patterns_strip_user_and_deduplicate(
    table: ModelLabHostsTable,
) -> None:
    scoped = ModelLabHostsTable(
        hosts=(
            table.hosts[0].model_copy(
                update={"target": "operator@alpha.example.invalid"}
            ),
            table.hosts[1].model_copy(update={"target": "beta"}),
        )
    )
    fake = FakeTransport(scoped)
    result = HandlerLabHostKeyDistribution(fake).handle(
        ModelLabHostKeyDistributionRequest(hosts=("beta", "alpha"), table=scoped)
    )
    assert result.exit_code == 0
    assert result.pairs[0].source == "beta"
    assert fake.files["beta"]["known_hosts"][0].startswith(
        "alpha,alpha.example.invalid "
    )
    assert fake.files["alpha"]["known_hosts"][0].startswith("beta ssh-")


def test_contract_and_models() -> None:
    contract = yaml.safe_load(
        NodeLabHostKeyDistributionEffect.contract_path.read_text()
    )
    handler = contract["handler"]
    assert (
        contract["handler_routing"]["default_handler"]
        == f"{handler['module']}:{handler['class']}"
    )
    assert handler["class"] == HandlerLabHostKeyDistribution.__name__
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["runtime_profiles"] == []
    assert contract["descriptor"]["idempotent"] is True
    assert contract["metadata"]["related_tickets"] == ["OMN-22115", "OMN-20348"]
    metadata = yaml.safe_load(
        NodeLabHostKeyDistributionEffect.contract_path.with_name(
            "metadata.yaml"
        ).read_text()
    )
    assert metadata["entry_points"]["onex.nodes"] == {
        "node_lab_host_key_distribution_effect": "omnimarket.nodes.node_lab_host_key_distribution_effect"
    }
    assert metadata["capabilities"]["requires_network"] is True
    assert metadata["capabilities"]["requires_secrets"] is False
    with pytest.raises(ValidationError):
        ModelLabHostKeyDistributionRequest(hosts=())
    with pytest.raises(ValidationError):
        ModelLabHostKeyDistributionRequest.model_validate(
            {"hosts": ["alpha"], "extra": True}
        )
    request = ModelLabHostKeyDistributionRequest(hosts=("alpha",))
    with pytest.raises(ValidationError):
        request.dry_run = True
