# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Container units (OMN-23390): a declared container gets a systemd user unit that restarts it after a stop."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_installer_container_units.__main__ import main
from omnimarket.nodes.node_host_installer_container_units.handlers.handler_container_units import (
    HandlerContainerUnits,
    render_unit,
)
from omnimarket.nodes.node_host_installer_container_units.models.model_container_units import (
    Mode,
    ModelContainerUnit,
    ModelContainerUnitsRequest,
)
from omnimarket.nodes.node_host_installer_container_units.node import (
    NodeHostInstallerContainerUnits,
)

pytestmark = pytest.mark.unit

UNIT = {
    "name": "onex-broker-a",
    "hosts": ["pool-a"],
    "container": "broker-a",
    "ticket": "OMN-23390",
    "docker_binary": "/usr/bin/docker",
    "restart_seconds": 10,
    "stop_timeout_seconds": 30,
    "probe_address": "127.0.0.1",
    "probe_port": 59092,
    "probe_wait_seconds": 5,
}


def _overlay(tmp_path: Path) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump({"units": [UNIT]}))
    return path


class _Recorder:
    def __init__(self, missing_container: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.missing_container = missing_container

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        code = 1 if self.missing_container and "inspect" in command else 0
        return subprocess.CompletedProcess(list(command), code, "", "")


def _request(
    tmp_path: Path, mode: Mode = "install", host: str = "pool-a"
) -> ModelContainerUnitsRequest:
    return ModelContainerUnitsRequest(
        host_name=host,
        unit="onex-broker-a",
        mode=mode,
        overlay_file=str(_overlay(tmp_path)),
    )


def test_the_rendered_unit_restarts_after_any_exit_and_attaches_to_a_running_container() -> (
    None
):
    text = render_unit(ModelContainerUnit(**UNIT))
    assert "ExecStart=/usr/bin/docker start -a broker-a\n" in text
    assert "Restart=always\nRestartSec=10\n" in text
    assert "ExecStop=/usr/bin/docker stop -t 30 broker-a\n" in text


def test_golden_chain_install_writes_the_unit_enables_it_and_reads_the_port(
    tmp_path: Path,
) -> None:
    recorder = _Recorder()
    probes: list[tuple[str, int, int]] = []

    def probe(address: str, port: int, wait: int) -> bool:
        probes.append((address, port, wait))
        return True

    node = NodeHostInstallerContainerUnits()
    node.handler = HandlerContainerUnits(run=recorder, probe=probe, home=tmp_path)
    result = node.handle(_request(tmp_path))
    assert result.exit_code == 0, result.stderr
    assert (tmp_path / ".config/systemd/user/onex-broker-a.service").is_file()
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-broker-a.service",
    ] in recorder.calls
    assert probes == [("127.0.0.1", 59092, 5)]
    again = node.handle(_request(tmp_path))
    assert "unchanged" in again.stdout


def test_error_chain_a_host_without_a_declared_unit_is_refused(tmp_path: Path) -> None:
    recorder = _Recorder()
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, host="pool-z"))
    assert result.exit_code == 1
    assert "none is declared" in result.stderr
    assert recorder.calls == []


def test_error_chain_a_missing_container_writes_nothing(tmp_path: Path) -> None:
    result = HandlerContainerUnits(
        run=_Recorder(missing_container=True), probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path))
    assert result.exit_code == 1
    assert "no container broker-a" in result.stderr
    assert not (tmp_path / ".config").exists()


def test_error_chain_a_port_that_stays_silent_fails_the_install(tmp_path: Path) -> None:
    result = HandlerContainerUnits(
        run=_Recorder(), probe=lambda *_: False, home=tmp_path
    ).handle(_request(tmp_path))
    assert result.exit_code == 1
    assert "does not answer after 5 s" in result.stderr


def test_status_reads_the_unit_and_the_port(tmp_path: Path) -> None:
    handler = HandlerContainerUnits(
        run=_Recorder(), probe=lambda *_: False, home=tmp_path
    )
    silent = handler.handle(_request(tmp_path, mode="status"))
    assert silent.exit_code == 1
    assert "SILENT 127.0.0.1:59092" in silent.stdout
    answering = HandlerContainerUnits(
        run=_Recorder(), probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, mode="status"))
    assert answering.exit_code == 0
    assert "LOADED onex-broker-a.service" in answering.stdout


def test_uninstall_removes_the_unit_and_leaves_the_container(tmp_path: Path) -> None:
    recorder = _Recorder()
    handler = HandlerContainerUnits(run=recorder, probe=lambda *_: True, home=tmp_path)
    handler.handle(_request(tmp_path))
    removed = handler.handle(_request(tmp_path, mode="uninstall"))
    assert removed.exit_code == 0
    assert not (tmp_path / ".config/systemd/user/onex-broker-a.service").exists()
    assert not any("docker" in call[0] and "stop" in call for call in recorder.calls)


def test_the_command_line_refuses_an_unreadable_overlay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "dry-run",
            "--host",
            "pool-a",
            "--unit",
            "onex-broker-a",
            "--overlay",
            str(tmp_path / "absent.yaml"),
        ]
    )
    assert code == 1
    assert "unreadable" in capsys.readouterr().err
