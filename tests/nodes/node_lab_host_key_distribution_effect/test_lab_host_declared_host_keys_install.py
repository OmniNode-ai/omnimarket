# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installing the public host keys the lab host table declares into known_hosts."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
from pydantic import ValidationError

from omnimarket.models.lab_host_key import EnumKeyFile, EnumLineState
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_lab_host_key_distribution_effect.handlers.handler_lab_host_declared_host_keys_install import (
    HandlerLabHostDeclaredHostKeysInstall,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.models import (
    EnumHostInstallStatus,
    ModelLabHostDeclaredHostKeysInstallRequest,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.protocols import (
    HostTransportError,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.runtime_lab_host_key_distribution import (
    main,
)

pytestmark = pytest.mark.unit


def _key(key_type: str = "ssh-ed25519") -> str:
    return f"{key_type} AAAA{key_type.replace('-', '')}0123456789abcdef"


class _Call(NamedTuple):
    host: str
    line: str
    apply: bool


class FakeTransport:
    """Records every ensure_line call; no host is ever reached."""

    def __init__(self) -> None:
        self.lines: dict[tuple[str, EnumKeyFile], list[str]] = {}
        self.calls: list[_Call] = []
        self.fail: set[str] = set()

    def ensure_line(
        self,
        host: ModelLabHostRow,
        key_file: EnumKeyFile,
        needle: str,
        line: str,
        *,
        apply: bool,
    ) -> EnumLineState:
        assert key_file == EnumKeyFile.KNOWN_HOSTS
        self.calls.append(_Call(host.name, line, apply))
        if host.name in self.fail:
            raise HostTransportError(f"ssh to {host.name} failed")
        bucket = self.lines.setdefault((host.name, key_file), [])
        if line in bucket:
            return EnumLineState.PRESENT
        if not apply:
            return EnumLineState.ABSENT
        bucket.append(line)
        return EnumLineState.ADDED


@pytest.fixture
def table() -> ModelLabHostsTable:
    return ModelLabHostsTable(
        hosts=(
            ModelLabHostRow(
                name="alpha",
                target="alpha.example.invalid",
                host_keys=(_key(), _key("ecdsa-sha2-nistp256")),
            ),
            ModelLabHostRow(
                name="beta",
                target="beta.example.invalid",
                fallback_targets=("beta-fallback",),
                host_keys=(_key(),),
            ),
            ModelLabHostRow(
                name="gamma",
                target="gamma.example.invalid",
                available=False,
                unavailable_reason="off the pool",
            ),
        )
    )


@pytest.fixture
def request_model(
    table: ModelLabHostsTable,
) -> ModelLabHostDeclaredHostKeysInstallRequest:
    return ModelLabHostDeclaredHostKeysInstallRequest(table=table)


def test_declared_host_keys_install_writes_one_line_per_destination_peer_key(
    request_model: ModelLabHostDeclaredHostKeysInstallRequest,
) -> None:
    fake = FakeTransport()
    first = HandlerLabHostDeclaredHostKeysInstall(fake).handle(request_model)
    assert first.exit_code == 0
    assert [(row.host, row.status) for row in first.hosts] == [
        ("alpha", EnumHostInstallStatus.OK),
        ("beta", EnumHostInstallStatus.OK),
        ("gamma", EnumHostInstallStatus.SKIPPED),
    ]
    assert first.hosts[0].keys_added == 1
    assert first.hosts[0].keys_present == 0
    assert first.hosts[1].keys_added == 2
    assert first.hosts[1].keys_present == 0
    assert fake.lines == {
        ("alpha", EnumKeyFile.KNOWN_HOSTS): [
            f"beta,beta.example.invalid,beta-fallback {_key()}"
        ],
        ("beta", EnumKeyFile.KNOWN_HOSTS): [
            f"alpha,alpha.example.invalid {_key()}",
            f"alpha,alpha.example.invalid {_key('ecdsa-sha2-nistp256')}",
        ],
    }


def test_declared_host_keys_install_second_run_adds_nothing(
    request_model: ModelLabHostDeclaredHostKeysInstallRequest,
) -> None:
    fake = FakeTransport()
    handler = HandlerLabHostDeclaredHostKeysInstall(fake)
    handler.handle(request_model)
    before = {bucket: list(lines) for bucket, lines in fake.lines.items()}
    second = handler.handle(request_model)
    assert second.exit_code == 0
    assert fake.lines == before
    assert second.hosts[0].keys_added == 0
    assert second.hosts[0].keys_present == 1
    assert second.hosts[1].keys_added == 0
    assert second.hosts[1].keys_present == 2


def test_declared_host_keys_install_dry_run_writes_nothing(
    request_model: ModelLabHostDeclaredHostKeysInstallRequest,
) -> None:
    fake = FakeTransport()
    result = HandlerLabHostDeclaredHostKeysInstall(fake).handle(
        request_model.model_copy(update={"dry_run": True})
    )
    assert result.exit_code == 0
    assert [row.status for row in result.hosts] == [
        EnumHostInstallStatus.PLANNED,
        EnumHostInstallStatus.PLANNED,
        EnumHostInstallStatus.SKIPPED,
    ]
    assert result.hosts[0].keys_added == 1
    assert result.lines()[0] == "alpha planned would-add=1 present=0"
    assert all(not written for written in fake.lines.values())


def test_declared_host_keys_install_named_scope_touches_only_that_host(
    request_model: ModelLabHostDeclaredHostKeysInstallRequest,
) -> None:
    fake = FakeTransport()
    result = HandlerLabHostDeclaredHostKeysInstall(fake).handle(
        request_model.model_copy(update={"hosts": ("beta",)})
    )
    assert [row.host for row in result.hosts] == ["beta"]
    assert {call.host for call in fake.calls} == {"beta"}


def test_one_refused_destination_keeps_the_others_ok(
    request_model: ModelLabHostDeclaredHostKeysInstallRequest,
) -> None:
    fake = FakeTransport()
    fake.fail.add("beta")
    result = HandlerLabHostDeclaredHostKeysInstall(fake).handle(request_model)
    assert result.exit_code == 1
    statuses = {row.host: row.status for row in result.hosts}
    assert statuses["alpha"] == EnumHostInstallStatus.OK
    assert statuses["beta"] == EnumHostInstallStatus.REFUSED
    assert statuses["gamma"] == EnumHostInstallStatus.SKIPPED
    assert "ssh to beta failed" in result.hosts[1].detail


def test_unknown_host_name_refuses(
    request_model: ModelLabHostDeclaredHostKeysInstallRequest,
) -> None:
    fake = FakeTransport()
    result = HandlerLabHostDeclaredHostKeysInstall(fake).handle(
        request_model.model_copy(update={"hosts": ("nowhere",)})
    )
    assert result.exit_code == 2
    assert "nowhere" in result.refusal
    assert fake.calls == []


def test_no_declared_host_keys_refuses() -> None:
    bare = ModelLabHostsTable(
        hosts=(ModelLabHostRow(name="alpha", target="alpha.example.invalid"),)
    )
    fake = FakeTransport()
    result = HandlerLabHostDeclaredHostKeysInstall(fake).handle(
        ModelLabHostDeclaredHostKeysInstallRequest(table=bare)
    )
    assert result.exit_code == 2
    assert result.refusal == "no lab host declares host_keys"
    assert fake.calls == []


def test_unavailable_destination_is_skipped_and_never_touched(
    request_model: ModelLabHostDeclaredHostKeysInstallRequest,
) -> None:
    fake = FakeTransport()
    result = HandlerLabHostDeclaredHostKeysInstall(fake).handle(
        request_model.model_copy(update={"hosts": ("gamma",)})
    )
    assert result.exit_code == 0
    assert [row.status for row in result.hosts] == [EnumHostInstallStatus.SKIPPED]
    assert result.hosts[0].detail == "unavailable: off the pool"
    assert fake.calls == []


def test_lab_host_row_declares_and_validates_host_keys() -> None:
    row = ModelLabHostRow(
        name="alpha",
        target="alpha.example.invalid",
        host_keys=(_key(), _key("ecdsa-sha2-nistp256")),
    )
    assert row.host_keys == (_key(), _key("ecdsa-sha2-nistp256"))
    with pytest.raises(ValidationError):
        ModelLabHostRow(
            name="alpha",
            target="alpha.example.invalid",
            host_keys=(_key() + " alpha@host",),
        )
    with pytest.raises(ValidationError):
        ModelLabHostRow(
            name="alpha",
            target="alpha.example.invalid",
            host_keys=(_key(), _key()),
        )
    with pytest.raises(ValidationError):
        ModelLabHostRow(
            name="alpha",
            target="alpha.example.invalid",
            host_keys=("ssh-ed25519 !!not-base64!!",),
        )


def test_a_table_document_without_host_keys_still_loads() -> None:
    # The seam with every older table document: the new column is optional.
    document = ModelLabHostsTable.model_validate(
        {"hosts": [{"name": "alpha", "target": "alpha.example.invalid"}]}
    )
    assert document.hosts[0].host_keys == ()


def test_every_fallback_name_of_a_declaring_row_lands_in_the_known_hosts_line(
    table: ModelLabHostsTable,
    request_model: ModelLabHostDeclaredHostKeysInstallRequest,
) -> None:
    # The launcher walks fallback_targets by name, so the line must match each name.
    fake = FakeTransport()
    HandlerLabHostDeclaredHostKeysInstall(fake).handle(
        request_model.model_copy(update={"hosts": ("alpha",)})
    )
    lines = fake.lines[("alpha", EnumKeyFile.KNOWN_HOSTS)]
    declaring = [row for row in table.hosts if row.host_keys and row.name != "alpha"]
    assert declaring
    for row in declaring:
        for name in row.fallback_targets:
            assert any(name in line for line in lines), name


def test_a_fallback_target_with_a_login_is_matched_by_its_host_name_only() -> None:
    # ssh_config style user@host destinations name the host after the last '@'; known_hosts patterns never hold the login.
    table = ModelLabHostsTable(
        hosts=(
            ModelLabHostRow(name="alpha", target="alpha.example.invalid"),
            ModelLabHostRow(
                name="beta",
                target="beta.example.invalid",
                fallback_targets=("ops@beta-fallback",),
                host_keys=(_key(),),
            ),
        )
    )
    fake = FakeTransport()
    HandlerLabHostDeclaredHostKeysInstall(fake).handle(
        ModelLabHostDeclaredHostKeysInstallRequest(table=table, hosts=("alpha",))
    )
    assert fake.lines[("alpha", EnumKeyFile.KNOWN_HOSTS)] == [
        f"beta,beta.example.invalid,beta-fallback {_key()}"
    ]


def test_declared_host_keys_parser_refuses_bad_flag_combinations() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--declared-keys", "--declared-host-keys"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["--declared-host-keys", "--allow-unavailable", "alpha"])
    assert exc.value.code == 2


@pytest.mark.parametrize("remote", [False, True], ids=["local", "ssh"])
def test_cli_declared_host_keys_writes_known_hosts(
    tmp_path: Path, remote: bool
) -> None:
    """Run the CLI and the real append script; only the SSH boundary is synthetic."""
    table = ModelLabHostsTable(
        hosts=(
            ModelLabHostRow(
                name="alpha",
                target="alpha.example.invalid",
                local=not remote,
                host_keys=(_key(),),
            ),
            ModelLabHostRow(
                name="beta",
                target="beta.example.invalid",
                available=False,
                unavailable_reason="off the pool",
                host_keys=(_key("ecdsa-sha2-nistp256"),),
            ),
        )
    )
    table_file = tmp_path / "table.yaml"
    table_file.write_text(table.model_dump_json(), encoding="utf-8")
    home = tmp_path / "host-home"
    home.mkdir()
    ssh_log = tmp_path / "ssh.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "assert sys.argv[1:6] == ['-o', 'BatchMode=yes', '-o', "
        "'ConnectTimeout=8', 'alpha.example.invalid']\n"
        "with Path(os.environ['SSH_PROBE_LOG']).open('a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "raise SystemExit(subprocess.run(['bash', '-c', sys.argv[6]], "
        "check=False).returncode)\n",
        encoding="utf-8",
    )
    ssh.chmod(0o700)
    environ = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "SSH_PROBE_LOG": str(ssh_log),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    argv = [
        sys.executable,
        "-m",
        "omnimarket.nodes.node_lab_host_key_distribution_effect.runtime_lab_host_key_distribution",
        "--declared-host-keys",
        "--table",
        str(table_file),
    ]

    def run(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*argv, *extra],
            env=environ,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    planned = run("--dry-run")
    assert planned.returncode == 0, planned.stderr
    assert planned.stdout.splitlines() == [
        "alpha planned would-add=1 present=0",
        "beta skipped would-add=0 present=0 unavailable: off the pool",
    ]
    known = home / ".ssh" / "known_hosts"
    assert not known.exists()

    first = run()
    assert first.returncode == 0, first.stderr
    assert first.stdout.splitlines() == [
        "alpha ok added=1 present=0",
        "beta skipped added=0 present=0 unavailable: off the pool",
    ]
    assert known.read_text().splitlines() == [
        f"beta,beta.example.invalid {_key('ecdsa-sha2-nistp256')}"
    ]
    assert stat.S_IMODE(known.stat().st_mode) == 0o600
    assert stat.S_IMODE(known.parent.stat().st_mode) == 0o700
    before = known.read_bytes()
    second = run()
    assert second.returncode == 0, second.stderr
    assert "alpha ok added=0 present=1" in second.stdout
    assert known.read_bytes() == before
    for result in (planned, first, second):
        assert result.stderr == ""
        assert _key() not in result.stdout
        assert _key("ecdsa-sha2-nistp256") not in result.stdout
        assert "example.invalid" not in result.stdout
    if remote:
        calls = [json.loads(line) for line in ssh_log.read_text().splitlines()]
        assert len(calls) == 3
        assert all(call[4] == "alpha.example.invalid" for call in calls)
        text = json.dumps(calls)
        assert _key() not in text
        assert _key("ecdsa-sha2-nistp256") not in text
    else:
        assert not ssh_log.exists()
