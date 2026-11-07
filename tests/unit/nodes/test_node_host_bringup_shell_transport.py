# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host transport of the bring-up run (OMN-19130).

The ssh and local adapter builds a batch-mode argv and sends the script on stdin;
the shell probe and actuator render each primitive and read its exit. Readings
here run the rendered scripts through the real local shell against a temporary
directory, so a wrong render reads wrong.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.handler_host_shell_scripted import (
    DOWN,
    NOT_OK,
    OK,
    HandlerHostShellScripted,
    UnscriptedCallError,
)
from omnimarket.handlers.handler_host_shell_transport import HandlerHostShellTransport
from omnimarket.models.host_bringup.model_host_shell import (
    EnumShellPrimitive,
    ModelDeclaredShellStep,
    ModelHostShellOverlay,
    ModelHostShellResult,
    ModelHostTransport,
)
from omnimarket.nodes.node_host_bringup_orchestrator.handlers.handler_shell_target_backends import (
    HandlerShellTargetActuator,
    HandlerShellTargetProbe,
    load_host_shell_overlay,
    render_apply,
    render_probe,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models.model_host_bringup_plan import (
    EnumObservedState,
    ModelBringupTarget,
)

pytestmark = pytest.mark.unit

SSH = ModelHostTransport(kind="ssh", target="h202")
LOCAL = ModelHostTransport(kind="local")
SENTINEL = "sentinel-value-7f3a"


def _target(name: str, action: str = "directory") -> ModelBringupTarget:
    return ModelBringupTarget.model_validate(
        {
            "name": name,
            "stage": "C",
            "check": {"kind": "file_state", "subject": name.replace("-", "_")},
            "falsifier": "x",
            "action": action,
        }
    )


def _overlay(
    host: str, transport: ModelHostTransport, *steps: dict[str, Any]
) -> ModelHostShellOverlay:
    return ModelHostShellOverlay.model_validate(
        {
            "ssh_options": ["StrictHostKeyChecking=yes"],
            "connect_timeout_s": 5,
            "probe_timeout_s": 10,
            "action_timeout_s": 20,
            "hosts": [
                {
                    "host": host,
                    "transport": transport.model_dump(),
                    "steps": list(steps),
                }
            ],
        }
    )


class _Recorder:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


def _local() -> HandlerHostShellTransport:
    return HandlerHostShellTransport(
        ssh_options=("StrictHostKeyChecking=yes",), connect_timeout_s=5
    )


# --- the ssh and local adapter ---------------------------------------------------------------


def test_ssh_argv_is_batch_mode_and_the_script_goes_on_stdin() -> None:
    rec = _Recorder()
    shell = HandlerHostShellTransport(
        ssh_options=("StrictHostKeyChecking=yes",), connect_timeout_s=7, runner=rec
    )
    shell.run(SSH, f"printf %s {SENTINEL}\n", privileged=True, timeout_s=9)
    argv, kw = rec.calls[0]
    assert argv == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=7",
        "-o",
        "StrictHostKeyChecking=yes",
        "h202",
        "sudo",
        "-n",
        "sh",
        "-s",
    ]
    assert all(SENTINEL not in a for a in argv)
    assert SENTINEL in kw["input"]
    assert kw["timeout"] == 9


def test_local_argv_runs_sh_on_the_launching_host() -> None:
    shell = _local()
    assert shell.argv(LOCAL, privileged=False) == ["sh", "-s"]
    assert shell.argv(LOCAL, privileged=True) == ["sudo", "-n", "sh", "-s"]


def test_ssh_exit_255_is_a_transport_failure_and_a_local_255_is_not() -> None:
    rec = _Recorder(returncode=255, stderr="Host key verification failed.\n")
    shell = HandlerHostShellTransport(
        ssh_options=("A=b",), connect_timeout_s=5, runner=rec
    )
    assert shell.run(SSH, "exit 0\n", privileged=False, timeout_s=5).transport_failed
    assert not shell.run(
        LOCAL, "exit 0\n", privileged=False, timeout_s=5
    ).transport_failed


def test_a_timeout_is_a_transport_failure() -> None:
    def runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    shell = HandlerHostShellTransport(
        ssh_options=("A=b",), connect_timeout_s=5, runner=runner
    )
    result = shell.run(SSH, "exit 0\n", privileged=False, timeout_s=3)
    assert result.transport_failed
    assert "3s" in result.stderr


def test_the_local_shell_really_runs_the_script() -> None:
    shell = _local()
    assert shell.run(LOCAL, "exit 0\n", privileged=False, timeout_s=10).exit_code == 0
    assert shell.run(LOCAL, "exit 1\n", privileged=False, timeout_s=10).exit_code == 1


# --- the scripted adapter --------------------------------------------------------------------


def test_scripted_refuses_a_script_nobody_scripted() -> None:
    shell = HandlerHostShellScripted(answers={})
    with pytest.raises(UnscriptedCallError):
        shell.run(
            LOCAL, "# onex-step lab-dirs probe\nexit 0\n", privileged=False, timeout_s=1
        )
    with pytest.raises(UnscriptedCallError):
        shell.run(LOCAL, "exit 0\n", privileged=False, timeout_s=1)


# --- the declared step model -----------------------------------------------------------------


def test_a_step_missing_its_parameter_is_refused_naming_it() -> None:
    with pytest.raises(ValidationError, match="needs path"):
        ModelDeclaredShellStep(target="lab-dirs", primitive="directory", mode="0755")


def test_a_step_with_a_foreign_parameter_is_refused() -> None:
    with pytest.raises(ValidationError, match="takes no path"):
        ModelDeclaredShellStep(
            target="hostname", primitive="hostname_is", expected="a", path="/x"
        )


def test_an_ssh_transport_without_a_target_is_refused() -> None:
    with pytest.raises(ValidationError, match="names its target"):
        ModelHostTransport(kind="ssh")


def test_the_committed_overlay_loads_and_declares_h202_and_h053() -> None:
    overlay = load_host_shell_overlay()
    hosts = {h.host: h for h in overlay.hosts}
    assert set(hosts) == {"h053", "h202"}
    assert [s.target for s in hosts["h202"].steps] == [
        "ssh-config",
        "key-login",
        "root-access",
    ]
    assert "data-moved" in [s.target for s in hosts["h053"].steps]
    assert "BatchMode=yes" not in overlay.ssh_options  # always added, never optional


# --- real readings through the local shell ---------------------------------------------------


def _probe(
    *steps: dict[str, Any], runner: Any = subprocess.run
) -> HandlerShellTargetProbe:
    shell = HandlerHostShellTransport(
        ssh_options=("A=b",), connect_timeout_s=5, runner=runner
    )
    return HandlerShellTargetProbe(shell=shell, overlay=_overlay("h053", LOCAL, *steps))


def test_directory_reads_not_ok_then_its_apply_makes_it_ok(tmp_path: Path) -> None:
    path = tmp_path / "lab"
    step = {
        "target": "lab-dirs",
        "primitive": "directory",
        "path": str(path),
        "mode": "0750",
    }
    probe = _probe(step)
    overlay = _overlay("h053", LOCAL, step)
    actuator = HandlerShellTargetActuator(shell=_local(), overlay=overlay)
    assert probe.read("h053", _target("lab-dirs")) is EnumObservedState.NOT_OK
    assert actuator.act("h053", _target("lab-dirs"), 1).ok
    assert probe.read("h053", _target("lab-dirs")) is EnumObservedState.OK
    assert stat.S_IMODE(path.stat().st_mode) == 0o750


def test_line_in_file_appends_once(tmp_path: Path) -> None:
    path = tmp_path / "conf"
    path.write_text("keep\n")
    step = {
        "target": "launcher-key",
        "primitive": "line_in_file",
        "path": str(path),
        "line": "a b 'c'",
    }
    probe = _probe(step)
    actuator = HandlerShellTargetActuator(
        shell=_local(), overlay=_overlay("h053", LOCAL, step)
    )
    target = _target("launcher-key", "line_in_file")
    assert probe.read("h053", target) is EnumObservedState.NOT_OK
    assert actuator.act("h053", target, 1).ok
    assert actuator.act("h053", target, 1).ok
    assert path.read_text() == "keep\na b 'c'\n"
    assert probe.read("h053", target) is EnumObservedState.OK


def test_hostname_is_reads_the_real_hostname() -> None:
    real = socket.gethostname()
    assert (
        _probe(
            {"target": "hostname", "primitive": "hostname_is", "expected": real}
        ).read("h053", _target("hostname"))
        is EnumObservedState.OK
    )
    assert (
        _probe(
            {"target": "hostname", "primitive": "hostname_is", "expected": real + "x"}
        ).read("h053", _target("hostname"))
        is EnumObservedState.NOT_OK
    )


def test_glob_absent_reads_a_planted_file(tmp_path: Path) -> None:
    step = {
        "target": "lane-env",
        "primitive": "glob_absent",
        "path": str(tmp_path),
        "pattern": "*.env.bak*",
    }
    probe = _probe(step)
    assert probe.read("h053", _target("lane-env")) is EnumObservedState.OK
    (tmp_path / "dev.env.bak.1").write_text("")
    assert probe.read("h053", _target("lane-env")) is EnumObservedState.NOT_OK


def _fake_findmnt(tmp_path: Path, lines: str) -> Any:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "findmnt"
    exe.write_text(f"#!/bin/sh\nprintf '%s\\n' {lines}\n")
    exe.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}

    def runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, env=env, **kw)

    return runner


_MOUNT = {
    "target": "data-moved",
    "primitive": "mount_source",
    "path": "/data/lab-run/runs",
    "fstype": "ext4",
    "source": "/dev/nvme0n1p1",
}


def test_mount_source_reads_the_topmost_mount(tmp_path: Path) -> None:
    runner = _fake_findmnt(
        tmp_path, "'tmpfs tmpfs' 'ext4 /dev/nvme0n1p1[/lab-run-runs]'"
    )
    assert (
        _probe(_MOUNT, runner=runner).read("h053", _target("data-moved"))
        is EnumObservedState.OK
    )


def test_mount_source_reads_the_ram_filesystem_on_top_as_not_ok(tmp_path: Path) -> None:
    runner = _fake_findmnt(
        tmp_path, "'ext4 /dev/nvme0n1p1[/lab-run-runs]' 'tmpfs tmpfs'"
    )
    assert (
        _probe(_MOUNT, runner=runner).read("h053", _target("data-moved"))
        is EnumObservedState.NOT_OK
    )


# --- unknown is never satisfied --------------------------------------------------------------


def test_a_target_with_no_declared_step_reads_unknown() -> None:
    probe = HandlerShellTargetProbe(
        shell=HandlerHostShellScripted(answers={}), overlay=_overlay("h202", SSH)
    )
    assert probe.read("h202", _target("lab-dirs")) is EnumObservedState.UNKNOWN
    assert probe.read("h999", _target("lab-dirs")) is EnumObservedState.UNKNOWN


@pytest.mark.parametrize(
    ("primitive", "result", "expected"),
    [
        ("transport_answers", DOWN, EnumObservedState.NOT_OK),
        ("privilege_answers", DOWN, EnumObservedState.UNKNOWN),
        ("privilege_answers", NOT_OK, EnumObservedState.NOT_OK),
        ("privilege_answers", OK, EnumObservedState.OK),
        (
            "privilege_answers",
            ModelHostShellResult(exit_code=2),
            EnumObservedState.UNKNOWN,
        ),
    ],
)
def test_exit_and_transport_map_to_observed_state(
    primitive: str, result: ModelHostShellResult, expected: EnumObservedState
) -> None:
    shell = HandlerHostShellScripted(answers={"root-access probe": [result]})
    probe = HandlerShellTargetProbe(
        shell=shell,
        overlay=_overlay(
            "h202", SSH, {"target": "root-access", "primitive": primitive}
        ),
    )
    assert probe.read("h202", _target("root-access")) is expected


def test_privilege_probe_runs_under_sudo_and_a_plain_probe_does_not() -> None:
    shell = HandlerHostShellScripted(
        answers={"root-access probe": [OK], "hostname probe": [OK]}
    )
    probe = HandlerShellTargetProbe(
        shell=shell,
        overlay=_overlay(
            "h202",
            SSH,
            {"target": "root-access", "primitive": "privilege_answers"},
            {
                "target": "hostname",
                "primitive": "hostname_is",
                "expected": "lab-host-a",
            },
        ),
    )
    probe.read("h202", _target("root-access"))
    probe.read("h202", _target("hostname"))
    assert [c[2] for c in shell.calls] == [True, False]


# --- the actuator refuses what it cannot do --------------------------------------------------


def test_actuator_refuses_an_undeclared_target_and_a_read_only_primitive() -> None:
    overlay = _overlay(
        "h202", SSH, {"target": "ssh-config", "primitive": "transport_answers"}
    )
    actuator = HandlerShellTargetActuator(
        shell=HandlerHostShellScripted(answers={}), overlay=overlay
    )
    missing = actuator.act("h202", _target("lab-dirs"), 1)
    assert not missing.ok
    assert "no declared step" in missing.detail
    readonly = actuator.act("h202", _target("ssh-config", "local_include"), 1)
    assert not readonly.ok
    assert "only reads" in readonly.detail


def test_a_failed_apply_names_expected_and_observed() -> None:
    step = {
        "target": "lab-dirs",
        "primitive": "directory",
        "path": "/srv/lab",
        "mode": "0755",
    }
    shell = HandlerHostShellScripted(
        answers={
            "lab-dirs act": [
                ModelHostShellResult(exit_code=1, stderr="install: denied\n")
            ]
        }
    )
    result = HandlerShellTargetActuator(
        shell=shell, overlay=_overlay("h202", SSH, step)
    ).act("h202", _target("lab-dirs"), 1)
    assert not result.ok
    assert "expected exit 0, observed 1" in result.detail
    assert "install: denied" in result.detail


def test_every_primitive_renders_a_probe_with_its_step_header() -> None:
    params = {
        "hostname_is": {"expected": "a"},
        "directory": {"path": "/a", "mode": "0755"},
        "line_in_file": {"path": "/a", "line": "x"},
        "service_active": {"unit": "docker.service"},
        "mount_source": {"path": "/a", "fstype": "ext4", "source": "/dev/x"},
        "glob_absent": {"path": "/a", "pattern": "*.bak"},
    }
    for primitive in EnumShellPrimitive:
        step = ModelDeclaredShellStep(
            target="t-x", primitive=primitive, **params.get(primitive.value, {})
        )
        assert render_probe(step).startswith("# onex-step t-x probe\n")
        apply = render_apply(step)
        assert apply is None or apply.startswith("# onex-step t-x act\n")
