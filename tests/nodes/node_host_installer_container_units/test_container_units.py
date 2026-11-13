# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Container units (OMN-23390): a declared container gets a systemd user unit that restarts it after a stop."""

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
    create_argv,
    memory_problem,
    render_unit,
)
from omnimarket.nodes.node_host_installer_container_units.models.model_container_units import (
    Mode,
    ModelContainerCreate,
    ModelContainerPort,
    ModelContainerUnit,
    ModelContainerUnitsRequest,
)
from omnimarket.nodes.node_host_installer_container_units.node import (
    NodeHostInstallerContainerUnits,
)
from omnimarket.nodes.node_host_installer_container_units.runtime_container_units import (
    main,
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
    "create": {
        "image": "example/broker:1.0",
        "command": ["broker", "start"],
        "volumes": ["example-broker:/data"],
        "restart_policy": "unless-stopped",
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


def _overlay(tmp_path: Path, unit: dict | None = None) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump({"units": [unit or UNIT]}))
    return path


class _Recorder:
    def __init__(
        self,
        missing_container: bool = False,
        inspect_stdout: str = "",
        inspect_fail_first: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.missing_container = missing_container
        self.inspect_stdout = inspect_stdout
        self.inspect_fail_first = inspect_fail_first
        self._inspect_count = 0

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        if "inspect" in command:
            self._inspect_count += 1
            if self.missing_container:
                return subprocess.CompletedProcess(list(command), 1, "", "")
            if self.inspect_fail_first and self._inspect_count == 1:
                return subprocess.CompletedProcess(list(command), 1, "", "")
            return subprocess.CompletedProcess(
                list(command), 0, self.inspect_stdout, ""
            )
        code = 1 if self.missing_container and "inspect" in command else 0
        return subprocess.CompletedProcess(list(command), code, "", "")


def _request(
    tmp_path: Path,
    mode: Mode = "install",
    host: str = "pool-a",
    unit: dict | None = None,
) -> ModelContainerUnitsRequest:
    return ModelContainerUnitsRequest(
        host_name=host,
        unit="onex-broker-a",
        mode=mode,
        overlay_file=str(_overlay(tmp_path, unit)),
    )


def test_the_rendered_unit_restarts_after_any_exit_and_attaches_to_a_running_container() -> (
    None
):
    text = render_unit(ModelContainerUnit(**UNIT))
    assert "ExecStart=/usr/bin/docker start -a broker-a\n" in text
    assert "Restart=always\nRestartSec=10\n" in text
    assert "ExecStop=/usr/bin/docker stop -t 30 broker-a\n" in text


def test_render_with_network_adds_exec_start_pre() -> None:
    text = render_unit(ModelContainerUnit(**UNIT_WITH_NETWORK))
    assert "ExecStartPre=-/usr/bin/docker network connect broker-net broker-a\n" in text
    assert text.index("ExecStartPre=") < text.index("ExecStart=")


def test_render_without_network_has_no_exec_start_pre() -> None:
    text = render_unit(ModelContainerUnit(**UNIT))
    assert "ExecStartPre" not in text


def test_create_argv_golden_with_ports_volume_and_env_file() -> None:
    unit = ModelContainerUnit(
        **UNIT,
        network="broker-net",
        published_ports=[
            ModelContainerPort(
                host_address="127.0.0.1", host_port=59092, container_port=59092
            ),
            ModelContainerPort(
                host_address="0.0.0.0", host_port=59093, container_port=59093
            ),
        ],
        create=ModelContainerCreate(
            image="example/broker:1.0",
            command=("broker", "start"),
            volumes=("example-broker:/data",),
            restart_policy="unless-stopped",
            env_file="broker.env",
        ),
    )
    assert create_argv(unit) == [
        "/usr/bin/docker",
        "create",
        "--name",
        "broker-a",
        "--network",
        "broker-net",
        "--restart",
        "unless-stopped",
        "-p",
        "127.0.0.1:59092:59092",
        "-p",
        "0.0.0.0:59093:59093",
        "-v",
        "example-broker:/data",
        "--env-file",
        "broker.env",
        "example/broker:1.0",
        "broker",
        "start",
    ]


def test_create_argv_golden_without_ports_volumes_or_env_file() -> None:
    unit = ModelContainerUnit(
        **UNIT,
        network="broker-net",
        create=ModelContainerCreate(
            image="example/broker:1.0",
            restart_policy="unless-stopped",
        ),
    )
    assert create_argv(unit) == [
        "/usr/bin/docker",
        "create",
        "--name",
        "broker-a",
        "--network",
        "broker-net",
        "--restart",
        "unless-stopped",
        "example/broker:1.0",
    ]


def test_golden_chain_install_with_network_writes_the_unit(tmp_path: Path) -> None:
    recorder = _Recorder(inspect_stdout=_healthy_inspect_json())
    probes: list[tuple[str, int, int]] = []

    def probe(address: str, port: int, wait: int) -> bool:
        probes.append((address, port, wait))
        return True

    node = NodeHostInstallerContainerUnits()
    node.handler = HandlerContainerUnits(run=recorder, probe=probe, home=tmp_path)
    result = node.handle(_request(tmp_path, unit=UNIT_WITH_NETWORK))
    assert result.exit_code == 0, result.stderr
    assert (tmp_path / ".config/systemd/user/onex-broker-a.service").is_file()
    assert probes == [("127.0.0.1", 59092, 5)]


def test_a_container_unit_whose_container_has_no_network_is_refused_at_install(
    tmp_path: Path,
) -> None:
    broken_json = json.dumps(
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
    recorder = _Recorder(inspect_stdout=broken_json)
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_NETWORK))
    assert result.exit_code == 1
    assert "has no attachment to network" in result.stderr
    assert not (tmp_path / ".config").exists()
    assert not any("systemctl" in call[0] for call in recorder.calls)


def test_network_mode_mismatch_is_refused(tmp_path: Path) -> None:
    broken_json = json.dumps(
        [
            {
                "HostConfig": {
                    "NetworkMode": "other-net",
                    "PortBindings": {
                        "59092/tcp": [{"HostIp": "127.0.0.1", "HostPort": "59092"}]
                    },
                },
                "State": {"Running": True},
                "NetworkSettings": {"Networks": {"broker-net": {}}},
            }
        ]
    )
    recorder = _Recorder(inspect_stdout=broken_json)
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_NETWORK))
    assert result.exit_code == 1
    assert "is on network other-net" in result.stderr
    assert not (tmp_path / ".config").exists()


def test_declared_port_missing_in_port_bindings_is_refused(tmp_path: Path) -> None:
    broken_json = json.dumps(
        [
            {
                "HostConfig": {
                    "NetworkMode": "broker-net",
                    "PortBindings": {},
                },
                "State": {"Running": True},
                "NetworkSettings": {"Networks": {"broker-net": {}}},
            }
        ]
    )
    recorder = _Recorder(inspect_stdout=broken_json)
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_NETWORK))
    assert result.exit_code == 1
    assert "does not publish" in result.stderr
    assert not (tmp_path / ".config").exists()


def test_empty_inspect_stdout_with_declared_network_is_refused(tmp_path: Path) -> None:
    recorder = _Recorder(inspect_stdout="")
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_NETWORK))
    assert result.exit_code == 1
    assert "printed no container document" in result.stderr
    assert not (tmp_path / ".config").exists()


def test_create_path_inspect_fails_then_create_succeeds(tmp_path: Path) -> None:
    recorder = _Recorder(
        inspect_stdout=_healthy_inspect_json(), inspect_fail_first=True
    )
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_NETWORK))
    assert result.exit_code == 0, result.stderr
    assert (tmp_path / ".config/systemd/user/onex-broker-a.service").is_file()
    create_calls = [
        c for c in recorder.calls if c[0] == "/usr/bin/docker" and "create" in c
    ]
    assert len(create_calls) == 1


def test_failing_create_returns_exit_1_and_writes_nothing(tmp_path: Path) -> None:
    class _FailingCreateRecorder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self._inspect_count = 0

        def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
            self.calls.append(list(command))
            if "inspect" in command:
                self._inspect_count += 1
                if self._inspect_count == 1:
                    return subprocess.CompletedProcess(list(command), 1, "", "")
                return subprocess.CompletedProcess(list(command), 0, "", "")
            if "create" in command:
                return subprocess.CompletedProcess(
                    list(command), 1, "", "create failed"
                )
            return subprocess.CompletedProcess(list(command), 0, "", "")

    recorder = _FailingCreateRecorder()
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_NETWORK))
    assert result.exit_code == 1
    assert "create failed" in result.stderr
    assert not (tmp_path / ".config").exists()


def test_model_refuses_published_ports_without_network() -> None:
    with pytest.raises(ValidationError):
        ModelContainerUnit(
            **UNIT,
            published_ports=[
                ModelContainerPort(
                    host_address="127.0.0.1", host_port=59092, container_port=59092
                )
            ],
        )


def test_model_refuses_create_without_network() -> None:
    with pytest.raises(ValidationError):
        ModelContainerUnit(
            **UNIT,
            create=ModelContainerCreate(
                image="example/broker:1.0", restart_policy="unless-stopped"
            ),
        )


def test_status_prints_network_attached(tmp_path: Path) -> None:
    recorder = _Recorder(inspect_stdout=_healthy_inspect_json())
    handler = HandlerContainerUnits(run=recorder, probe=lambda *_: True, home=tmp_path)
    result = handler.handle(_request(tmp_path, mode="status", unit=UNIT_WITH_NETWORK))
    assert "NETWORK ATTACHED broker-net" in result.stdout
    assert result.exit_code == 0


def test_status_prints_network_problem(tmp_path: Path) -> None:
    broken_json = json.dumps(
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
    recorder = _Recorder(inspect_stdout=broken_json)
    handler = HandlerContainerUnits(run=recorder, probe=lambda *_: True, home=tmp_path)
    result = handler.handle(_request(tmp_path, mode="status", unit=UNIT_WITH_NETWORK))
    assert "NETWORK PROBLEM" in result.stdout
    assert result.exit_code == 1


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


GIB = 1024**3

UNIT_WITH_MEMORY = {**UNIT, "memory_gib": 4, "memory_swap_gib": 8}


def _memory_inspect_json(
    memory_bytes: int, swap_bytes: int, network: str | None = "broker-net"
) -> str:
    host_config: dict = {"Memory": memory_bytes, "MemorySwap": swap_bytes}
    document: dict = {"HostConfig": host_config}
    if network is not None:
        host_config["NetworkMode"] = network
        host_config["PortBindings"] = {
            "59092/tcp": [{"HostIp": "127.0.0.1", "HostPort": "59092"}]
        }
        document["State"] = {"Running": True}
        document["NetworkSettings"] = {"Networks": {network: {}}}
    return json.dumps([document])


def test_memory_problem_returns_none_when_the_limits_already_match() -> None:
    unit = ModelContainerUnit(**UNIT_WITH_MEMORY)
    assert (
        memory_problem(unit, _memory_inspect_json(4 * GIB, 8 * GIB, network=None))
        is None
    )


def test_memory_problem_returns_the_declared_bytes_when_the_container_runs_with_0_0() -> (
    None
):
    unit = ModelContainerUnit(**UNIT_WITH_MEMORY)
    assert memory_problem(unit, _memory_inspect_json(0, 0, network=None)) == (
        4 * GIB,
        8 * GIB,
    )


def test_memory_problem_raises_on_garbage_inspect_stdout() -> None:
    unit = ModelContainerUnit(**UNIT_WITH_MEMORY)
    with pytest.raises(ValueError, match="printed no container document"):
        memory_problem(unit, "not json")


def test_memory_problem_ignores_the_inspect_when_no_memory_is_declared() -> None:
    assert memory_problem(ModelContainerUnit(**UNIT), "") is None


def test_model_refuses_memory_gib_without_memory_swap_gib() -> None:
    with pytest.raises(ValidationError):
        ModelContainerUnit(**UNIT, memory_gib=4)


def test_model_refuses_memory_swap_gib_without_memory_gib() -> None:
    with pytest.raises(ValidationError):
        ModelContainerUnit(**UNIT, memory_swap_gib=8)


def test_model_refuses_memory_swap_gib_below_memory_gib() -> None:
    with pytest.raises(ValidationError):
        ModelContainerUnit(**UNIT, memory_gib=8, memory_swap_gib=4)


def test_golden_chain_install_with_a_memory_limit_updates_the_container_in_place(
    tmp_path: Path,
) -> None:
    recorder = _Recorder(
        inspect_stdout=_memory_inspect_json(2 * GIB, 2 * GIB, network=None)
    )
    probes: list[tuple[str, int, int]] = []

    def probe(address: str, port: int, wait: int) -> bool:
        probes.append((address, port, wait))
        return True

    node = NodeHostInstallerContainerUnits()
    node.handler = HandlerContainerUnits(run=recorder, probe=probe, home=tmp_path)
    result = node.handle(_request(tmp_path, unit=UNIT_WITH_MEMORY))
    assert result.exit_code == 0, result.stderr
    assert [call for call in recorder.calls if "update" in call] == [
        [
            "/usr/bin/docker",
            "update",
            "--memory",
            str(4 * GIB),
            "--memory-swap",
            str(8 * GIB),
            "broker-a",
        ]
    ]
    assert (tmp_path / ".config/systemd/user/onex-broker-a.service").is_file()
    assert (
        "set memory limit of broker-a to 4 GiB, memory plus swap 8 GiB" in result.stdout
    )
    assert probes == [("127.0.0.1", 59092, 5)]


def test_install_with_matching_memory_limits_makes_no_update_call(
    tmp_path: Path,
) -> None:
    recorder = _Recorder(
        inspect_stdout=_memory_inspect_json(4 * GIB, 8 * GIB, network=None)
    )
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_MEMORY))
    assert result.exit_code == 0, result.stderr
    assert not any("update" in call for call in recorder.calls)
    assert "set memory limit" not in result.stdout
    assert (tmp_path / ".config/systemd/user/onex-broker-a.service").is_file()


def test_error_chain_a_failed_memory_update_writes_no_unit_file(tmp_path: Path) -> None:
    class _FailingUpdateRecorder(_Recorder):
        def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
            if "update" in command:
                self.calls.append(list(command))
                return subprocess.CompletedProcess(
                    list(command), 1, "", "update failed"
                )
            return super().__call__(command)

    recorder = _FailingUpdateRecorder(
        inspect_stdout=_memory_inspect_json(2 * GIB, 2 * GIB, network=None)
    )
    result = HandlerContainerUnits(
        run=recorder, probe=lambda *_: True, home=tmp_path
    ).handle(_request(tmp_path, unit=UNIT_WITH_MEMORY))
    assert result.exit_code == 1
    assert (
        "docker update failed (exit 1): update failed; nothing written" in result.stderr
    )
    assert not (tmp_path / ".config").exists()


def test_status_reports_memory_limit_differs(tmp_path: Path) -> None:
    recorder = _Recorder(
        inspect_stdout=_memory_inspect_json(2 * GIB, 2 * GIB, network="broker-net")
    )
    handler = HandlerContainerUnits(run=recorder, probe=lambda *_: True, home=tmp_path)
    result = handler.handle(
        _request(
            tmp_path,
            mode="status",
            unit={**UNIT_WITH_NETWORK, "memory_gib": 4, "memory_swap_gib": 8},
        )
    )
    assert "NETWORK ATTACHED broker-net" in result.stdout
    assert (
        "MEMORY LIMIT DIFFERS declared 4 GiB, running 2147483648 bytes" in result.stdout
    )
    assert result.exit_code == 1
    assert not any("update" in call for call in recorder.calls)


def test_status_reports_memory_limit_ok(tmp_path: Path) -> None:
    recorder = _Recorder(
        inspect_stdout=_memory_inspect_json(4 * GIB, 8 * GIB, network=None)
    )
    handler = HandlerContainerUnits(run=recorder, probe=lambda *_: True, home=tmp_path)
    result = handler.handle(_request(tmp_path, mode="status", unit=UNIT_WITH_MEMORY))
    assert "MEMORY LIMIT OK 4 GiB" in result.stdout
    assert result.exit_code == 0
    assert any("inspect" in call for call in recorder.calls)


def test_memory_an_unreadable_inspect_refuses_the_install_and_writes_nothing(
    tmp_path: Path,
) -> None:
    unit = ModelContainerUnit(
        name="onex-cache",
        hosts=("h-test",),
        container="cache",
        ticket="OMN-24208",
        docker_binary="/usr/bin/docker",
        restart_seconds=10,
        stop_timeout_seconds=30,
        probe_address="127.0.0.1",
        probe_port=3141,
        probe_wait_seconds=1,
        memory_gib=4,
        memory_swap_gib=8,
    )
    handler = HandlerContainerUnits(
        run=lambda argv: subprocess.CompletedProcess(list(argv), 0, "not json", ""),
        probe=lambda *_: True,
        home=tmp_path,
    )

    result = handler._install(
        unit, "onex-cache.service", tmp_path / "onex-cache.service", "text"
    )

    assert result.exit_code == 1
    assert "no container document" in result.stderr
    assert not (tmp_path / "onex-cache.service").exists()
