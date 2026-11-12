# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe effect's behaviour: what it read, and why it could not read (OMN-24027)."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.nodes.node_branch_deploy_probe_effect.adapters.adapter_in_memory_probe import (
    InMemoryBranchDeployProbe,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.handlers.handler_branch_deploy_probe import (
    HandlerBranchDeployProbe,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters.adapter_lab_facts import (
    RuntimeProbeSource,
)
from tests.nodes.node_branch_deploy_probe_effect.support import (
    HEAD,
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


def test_real_source_read_with_fake_fetch_and_run() -> None:
    settings = overlay().probe

    def fetch(url: str, timeout: int) -> str:
        assert url == settings.health_url
        return json.dumps({"details": {"healthy": True}})

    def run(argv: Any, stdin: Any, timeout: int) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"vcs_ref": HEAD}).encode(), stderr=b""
        )

    source = RuntimeProbeSource(settings, {settings.ssh_host_env: "host"}, fetch, run)
    result = _handler(source).handle(request())
    assert result.status == "read"
    assert result.healthy is True
    assert result.observed_sha == HEAD


def test_real_source_unreadable_when_run_fails() -> None:
    settings = overlay().probe

    def fetch(url: str, timeout: int) -> str:
        return json.dumps({"details": {"healthy": True}})

    def run(argv: Any, stdin: Any, timeout: int) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"boom")

    source = RuntimeProbeSource(settings, {settings.ssh_host_env: "host"}, fetch, run)
    result = _handler(source).handle(request())
    assert result.status == "unreadable"
    assert result.healthy is False
    assert result.observed_sha == ""
