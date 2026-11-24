# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe routes write their own state under the runtime's declared writable state root, never under the registry root.

The dev runtime container mounts the registry root (OMNI_HOME=/app) read-only and declares its writable state root in
ONEX_STATE_DIR / ONEX_STATE_ROOT (/app/data/.onex_state). The default C13, C14 and C17 routes handed the probe tick
``--state-dir {registry_root}/.onex_state/delegation-check-probe``, so the tick wrote ``/app/.onex_state`` and died with a
PermissionError (exit 1), which the executor reads as a failed check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    ModelBuildDelegationChecksOverlay,
)
from omnimarket.nodes.node_build_delegation_checks.adapters import (
    command_check_executor,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.command_check_executor import (
    CommandCheckExecutor,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumCheckReading,
)

BUILD = "a" * 40
PROBE_CHECKS = ("C13", "C14", "C17")


class _Capture:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], **_: Any
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")


def _state_dir_argument(command: list[str]) -> str:
    return command[command.index("--state-dir") + 1]


def test_default_probe_routes_name_the_state_root_and_never_the_registry_root() -> None:
    routes = ModelBuildDelegationChecksOverlay().check_routes
    for check in PROBE_CHECKS:
        state_dir = _state_dir_argument(list(routes[check]))
        assert state_dir.startswith("{state_root}/"), (check, state_dir)
        assert "{registry_root}" not in state_dir, (check, state_dir)
        assert ".onex_state" not in state_dir, (check, state_dir)


def test_the_probe_gets_a_state_dir_under_the_injected_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _Capture()
    monkeypatch.setattr(command_check_executor.subprocess, "run", capture)
    registry = tmp_path / "app"
    state = tmp_path / "data" / ".onex_state"
    runner = CommandCheckExecutor(
        ModelBuildDelegationChecksOverlay().check_routes,
        registry_root=registry,
        state_root=state,
    )

    for check in PROBE_CHECKS:
        assert runner.run(check, BUILD, 5).reading is EnumCheckReading.PASS

    assert len(capture.commands) == len(PROBE_CHECKS)
    for command in capture.commands:
        state_dir = Path(_state_dir_argument(command))
        assert state_dir == state / "delegation-check-probe"
        assert not state_dir.is_relative_to(registry)
        assert command[command.index("--omni-home") + 1] == str(registry)


def test_a_route_naming_the_state_root_with_none_injected_reads_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _Capture()
    monkeypatch.setattr(command_check_executor.subprocess, "run", capture)
    runner = CommandCheckExecutor(
        ModelBuildDelegationChecksOverlay().check_routes, registry_root=tmp_path
    )

    outcome = runner.run("C14", BUILD, 5)

    assert outcome.reading is EnumCheckReading.UNAVAILABLE
    assert "state root" in outcome.detail
    assert capture.commands == [], "no probe may start with an unresolved state dir"


def test_a_route_that_names_no_state_root_needs_none() -> None:
    runner = CommandCheckExecutor({"C11": ("true",)})
    assert runner.run("C11", BUILD, 5).reading is EnumCheckReading.PASS


def test_the_handler_built_from_the_overlay_passes_the_declared_state_root_to_the_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_build_delegation_checks.adapters.bus_sink import (
        BusRegressionSink,
    )
    from omnimarket.nodes.node_build_delegation_checks.handlers.handler_build_delegation_checks import (
        OMNI_HOME_ENV,
        REPLAY_ENV,
        HandlerBuildDelegationChecks,
    )
    from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
        EnumBuildChecksStatus,
        ModelRebuildCompletedObserved,
    )

    capture = _Capture()
    monkeypatch.setattr(command_check_executor.subprocess, "run", capture)
    home = tmp_path / "app"
    home.mkdir()
    state = tmp_path / "data" / ".onex_state"
    state.mkdir(parents=True)
    monkeypatch.setenv(OMNI_HOME_ENV, str(home))
    monkeypatch.delenv(REPLAY_ENV, raising=False)
    monkeypatch.setenv("ONEX_STATE_DIR", str(state))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)

    async def swallow(
        self: BusRegressionSink, topic: str, key: str, value: bytes
    ) -> None:
        return None

    monkeypatch.setattr(BusRegressionSink, "_send", swallow)

    result = HandlerBuildDelegationChecks().handle(
        ModelRebuildCompletedObserved(git_sha=BUILD, runtime_lane="dev")
    )

    assert result.status is EnumBuildChecksStatus.RAN
    probes = [c for c in capture.commands if "--state-dir" in c]
    assert {c[c.index("--check") + 1] for c in probes} == set(PROBE_CHECKS)
    for command in probes:
        state_dir = Path(_state_dir_argument(command))
        assert state_dir == state / "delegation-check-probe"
        assert not state_dir.is_relative_to(home)
