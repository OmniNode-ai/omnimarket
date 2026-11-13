# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe effect's behaviour: what it read, and why it could not read (OMN-24027)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_branch_deploy_probe_effect.adapters.adapter_in_memory_probe import (
    InMemoryBranchDeployProbe,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.adapters.adapter_runtime_probe import (
    RuntimeBuildProbe,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.handlers.handler_branch_deploy_probe import (
    HandlerBranchDeployProbe,
)
from tests.nodes.node_branch_deploy_probe_effect.support import (
    HEAD,
    NODE_DIR,
    fact,
    overlay,
    request,
)

pytestmark = pytest.mark.unit

PROBED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _handler(
    probe: InMemoryBranchDeployProbe,
    clock: Any = None,
) -> HandlerBranchDeployProbe:
    return HandlerBranchDeployProbe(
        overlay=overlay(),
        probe=probe,
        environ={},
        clock=clock if clock is not None else (lambda: PROBED_AT),
    )


def test_golden_probe_read_matching() -> None:
    probe = InMemoryBranchDeployProbe(facts=[((fact(HEAD, True),), None)])
    result = _handler(probe).handle(request())
    assert result.status == "read"
    assert result.healthy is True
    assert result.observed_sha == HEAD
    assert result.expected_sha == HEAD
    assert result.detail == ""


def test_golden_probe_read_other_sha() -> None:
    observed = "b" * 40
    probe = InMemoryBranchDeployProbe(facts=[((fact(observed, True),), None)])
    result = _handler(probe).handle(request())
    assert result.status == "read"
    assert result.observed_sha == observed
    assert result.expected_sha == HEAD


def test_error_chain_unreadable() -> None:
    probe = InMemoryBranchDeployProbe(
        facts=[((), "runtime probe unreadable: ValueError")]
    )
    result = _handler(probe).handle(request())
    assert result.status == "unreadable"
    assert result.healthy is False
    assert result.observed_sha == ""
    assert result.detail == "runtime probe unreadable: ValueError"


def test_error_chain_unhealthy() -> None:
    probe = InMemoryBranchDeployProbe(facts=[((fact(HEAD, False),), None)])
    result = _handler(probe).handle(request())
    assert result.status == "read"
    assert result.healthy is False
    assert result.observed_sha == HEAD


def test_injected_clock_reaches_the_probe() -> None:
    probe = InMemoryBranchDeployProbe(facts=[((fact(HEAD, True),), None)])
    stamp = datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC)
    _handler(probe, clock=lambda: stamp).handle(request())
    assert probe.calls == [(stamp, HEAD)]


def test_control_swapped_shas_differ() -> None:
    observed = "b" * 40
    probe = InMemoryBranchDeployProbe(facts=[((fact(observed, True),), None)])
    result = _handler(probe).handle(request())
    assert result.observed_sha != result.expected_sha


def test_runtime_build_probe_read_with_observed_sha() -> None:
    settings = overlay().probe
    calls: list[Any] = []

    def run(argv: Any, stdin: Any, timeout: int) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, stdin, timeout))
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b'{"details": {"healthy": true}}\n---\n{"vcs_ref": "'
            + HEAD.encode()
            + b'"}\n',
        )

    probe = RuntimeBuildProbe(settings, {settings.ssh_host_env: "host"}, run)
    result = HandlerBranchDeployProbe(
        overlay=overlay(),
        probe=probe,
        environ={},
        clock=lambda: PROBED_AT,
    ).handle(request())
    assert result.status == "read"
    assert result.healthy is True
    assert result.observed_sha == HEAD
    assert len(calls) == 1
    argv, stdin, timeout = calls[0]
    assert argv == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        "host",
        "bash -s",
    ]
    assert timeout == settings.timeout_seconds
    script = stdin.decode()
    assert settings.health_url in script
    assert f'docker exec -i "$RT" cat {settings.provenance_path}' in script


def test_runtime_build_probe_unhealthy_flag_false() -> None:
    settings = overlay().probe

    def run(argv: Any, stdin: Any, timeout: int) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b'{"details": {"healthy": false}}\n---\n{"vcs_ref": "'
            + HEAD.encode()
            + b'"}\n',
        )

    probe = RuntimeBuildProbe(settings, {settings.ssh_host_env: "host"}, run)
    result = HandlerBranchDeployProbe(
        overlay=overlay(),
        probe=probe,
        environ={},
        clock=lambda: PROBED_AT,
    ).handle(request())
    assert result.status == "read"
    assert result.healthy is False
    assert result.observed_sha == HEAD


def test_runtime_build_probe_unreadable_nonzero_exit() -> None:
    settings = overlay().probe

    def run(argv: Any, stdin: Any, timeout: int) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout=b"")

    probe = RuntimeBuildProbe(settings, {settings.ssh_host_env: "host"}, run)
    result = HandlerBranchDeployProbe(
        overlay=overlay(),
        probe=probe,
        environ={},
        clock=lambda: PROBED_AT,
    ).handle(request())
    assert result.status == "unreadable"
    assert result.healthy is False
    assert result.observed_sha == ""
    assert "exited 1" in result.detail


def test_runtime_build_probe_unreadable_no_separator() -> None:
    settings = overlay().probe

    def run(argv: Any, stdin: Any, timeout: int) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b'{"details": {"healthy": true}}\n{"vcs_ref": "'
            + HEAD.encode()
            + b'"}\n',
        )

    probe = RuntimeBuildProbe(settings, {settings.ssh_host_env: "host"}, run)
    result = HandlerBranchDeployProbe(
        overlay=overlay(),
        probe=probe,
        environ={},
        clock=lambda: PROBED_AT,
    ).handle(request())
    assert result.status == "unreadable"
    assert result.healthy is False
    assert result.observed_sha == ""


def test_runtime_build_probe_unreadable_host_unset() -> None:
    settings = overlay().probe

    def run(argv: Any, stdin: Any, timeout: int) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("run must not be called when the host is unset")

    probe = RuntimeBuildProbe(settings, {}, run)
    result = HandlerBranchDeployProbe(
        overlay=overlay(),
        probe=probe,
        environ={},
        clock=lambda: PROBED_AT,
    ).handle(request())
    assert result.status == "unreadable"
    assert result.healthy is False
    assert result.observed_sha == ""


def test_node_does_not_import_adapter_lab_facts() -> None:
    for path in NODE_DIR.rglob("*.py"):
        text = path.read_text()
        assert "adapter_lab_facts" not in text


def test_the_ssh_script_is_valid_shell(tmp_path: Path) -> None:
    """The script sent to the runtime host parses: a bracket missing its space once passed every other test."""
    seen: dict[str, str] = {}

    def run(argv, stdin, timeout):
        seen["script"] = (stdin or b"").decode()
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    settings = overlay().probe
    RuntimeBuildProbe(settings, {settings.ssh_host_env: "host-a"}, run).read(
        PROBED_AT, HEAD
    )
    script = tmp_path / "probe.sh"
    script.write_text(seen["script"])
    checked = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert checked.returncode == 0, checked.stderr
