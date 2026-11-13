# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recreate of a compose-managed container whose network or published port is wrong (OMN-24249)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_host_installer_container_units.handlers.handler_container_units import (
    HandlerContainerUnits,
    recreate_argv,
)
from omnimarket.nodes.node_host_installer_container_units.models.model_container_units import (
    ModelContainerRecreate,
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

UNIT_WITH_NETWORK = {
    **UNIT,
    "network": "broker-net",
    "published_ports": [
        {"host_address": "127.0.0.1", "host_port": 59092, "container_port": 59092}
    ],
}

UNIT_WITH_RECREATE = {
    **UNIT_WITH_NETWORK,
    "recreate": {
        "compose_file": "/etc/omni/broker-compose.yaml",
        "env_file": "/etc/omni/broker.env",
        "project": "broker",
        "service": "broker",
    },
}


def _healthy_inspect_json(network: str = "broker-net") -> str:
    return json.dumps(
        [
            {
                "HostConfig": {
                    "NetworkMode": network,
                    "PortBindings": {
                        "59092/tcp": [{"HostIp": "127.0.0.1", "HostPort": "59092"}]
                    },
                },
                "State": {"Running": True},
                "NetworkSettings": {"Networks": {network: {}}},
            }
        ]
    )


def _detached_inspect_json() -> str:
    return json.dumps(
        [
            {
                "HostConfig": {
                    "NetworkMode": "broker-net",
                    "PortBindings": {
                        "59092/tcp": [{"HostIp": "127.0.0.1", "HostPort": "59092"}]
                    },
                },
                "State": {"Running": True},
                "NetworkSettings": {"Networks": {}},
            }
        ]
    )


def _overlay(tmp_path: Path, unit: dict | None = None) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump({"units": [unit or UNIT]}))
    return path


class _Recorder:
    def __init__(
        self,
        inspect_sequence: list[str] | None = None,
        recreate_fail: bool = False,
        recreate_stderr: str = "recreate failed",
    ) -> None:
        self.calls: list[list[str]] = []
        self.inspect_sequence = inspect_sequence or []
        self.recreate_fail = recreate_fail
        self.recreate_stderr = recreate_stderr
        self._inspect_count = 0

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        if "inspect" in command:
            self._inspect_count += 1
            index = self._inspect_count - 1
            stdout = (
                self.inspect_sequence[index]
                if index < len(self.inspect_sequence)
                else (self.inspect_sequence[-1] if self.inspect_sequence else "")
            )
            return subprocess.CompletedProcess(list(command), 0, stdout, "")
        if "compose" in command:
            if self.recreate_fail:
                return subprocess.CompletedProcess(
                    list(command), 1, "", self.recreate_stderr
                )
            return subprocess.CompletedProcess(list(command), 0, "", "")
        return subprocess.CompletedProcess(list(command), 0, "", "")


def _request(tmp_path: Path, unit: dict | None = None) -> ModelContainerUnitsRequest:
    return ModelContainerUnitsRequest(
        host_name="pool-a",
        unit="onex-broker-a",
        mode="install",
        overlay_file=str(_overlay(tmp_path, unit)),
    )


def test_recreate_argv_golden() -> None:
    unit = ModelContainerUnit(
        **UNIT_WITH_NETWORK,
        recreate=ModelContainerRecreate(
            compose_file="/etc/omni/broker-compose.yaml",
            env_file="/etc/omni/broker.env",
            project="broker",
            service="broker",
        ),
    )
    assert recreate_argv(unit) == [
        "/usr/bin/docker",
        "compose",
        "--project-name",
        "broker",
        "--env-file",
        "/etc/omni/broker.env",
        "-f",
        "/etc/omni/broker-compose.yaml",
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "broker",
    ]


def test_recreate_argv_raises_when_not_declared() -> None:
    unit = ModelContainerUnit(**UNIT_WITH_NETWORK)
    with pytest.raises(ValueError, match="recreate is not declared for this unit"):
        recreate_argv(unit)


def test_golden_chain_detached_container_is_recreated_and_the_unit_written(
    tmp_path: Path,
) -> None:
    recorder = _Recorder(
        inspect_sequence=[_detached_inspect_json(), _healthy_inspect_json()]
    )
    probes: list[tuple[str, int, int]] = []

    def probe(address: str, port: int, wait: int) -> bool:
        probes.append((address, port, wait))
        return True

    node = NodeHostInstallerContainerUnits()
    node.handler = HandlerContainerUnits(run=recorder, probe=probe, home=tmp_path)
    result = node.handle(_request(tmp_path, unit=UNIT_WITH_RECREATE))
    assert result.exit_code == 0, result.stderr
    compose_calls = [c for c in recorder.calls if "compose" in c]
    assert len(compose_calls) == 1
    assert compose_calls[0] == [
        "/usr/bin/docker",
        "compose",
        "--project-name",
        "broker",
        "--env-file",
        "/etc/omni/broker.env",
        "-f",
        "/etc/omni/broker-compose.yaml",
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "broker",
    ]
    assert result.stdout.startswith(
        "recreated broker-a through compose project broker because:"
    )
    assert (tmp_path / ".config/systemd/user/onex-broker-a.service").is_file()
    assert probes == [("127.0.0.1", 59092, 5)]


def test_healthy_container_is_never_recreated(tmp_path: Path) -> None:
    recorder = _Recorder(inspect_sequence=[_healthy_inspect_json()])
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_RECREATE))
    assert result.exit_code == 0, result.stderr
    assert not any("compose" in c for c in recorder.calls)
    assert (tmp_path / ".config/systemd/user/onex-broker-a.service").is_file()


def test_no_recreate_declared_is_still_refused(tmp_path: Path) -> None:
    recorder = _Recorder(inspect_sequence=[_detached_inspect_json()])
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_NETWORK))
    assert result.exit_code == 1
    assert "has no attachment to network" in result.stderr
    assert "recreate the container from its declared create settings" in result.stderr
    assert not (tmp_path / ".config").exists()
    assert not any("compose" in c for c in recorder.calls)


def test_error_chain_failed_recreate_writes_nothing(tmp_path: Path) -> None:
    recorder = _Recorder(
        inspect_sequence=[_detached_inspect_json()], recreate_fail=True
    )
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_RECREATE))
    assert result.exit_code == 1
    assert "recreate failed (exit 1): recreate failed; nothing written" in result.stderr
    assert not (tmp_path / ".config").exists()


def test_error_chain_still_detached_after_recreate_writes_nothing(
    tmp_path: Path,
) -> None:
    recorder = _Recorder(inspect_sequence=[_detached_inspect_json()])
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_RECREATE))
    assert result.exit_code == 1
    assert "after the recreate; nothing written" in result.stderr
    assert not (tmp_path / ".config").exists()


def test_model_refuses_recreate_without_network() -> None:
    with pytest.raises(ValidationError):
        ModelContainerUnit(
            **UNIT,
            recreate=ModelContainerRecreate(
                compose_file="c.yaml",
                env_file="e.env",
                project="p",
                service="s",
            ),
        )


def test_model_refuses_an_unknown_recreate_key() -> None:
    with pytest.raises(ValidationError):
        ModelContainerUnit(
            **UNIT_WITH_NETWORK,
            recreate={
                "compose_file": "c.yaml",
                "env_file": "e.env",
                "project": "p",
                "service": "s",
                "bogus": "x",
            },
        )
