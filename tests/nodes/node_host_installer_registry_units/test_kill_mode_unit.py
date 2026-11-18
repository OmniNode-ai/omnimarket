# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: systemd registry units may carry an optional kill_mode (OMN-24448).

A systemd registry unit declared in the installer contract may carry an optional field kill_mode (values process or mixed).
HandlerRegistryUnits.render_systemd appends it right after the StandardError line.
The unit host-drift-owner-dispatch-systemd declares kill_mode: process.
A launchd unit declaring kill_mode is refused with ValueError matching 'launchd units do not support'.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)
from tests.nodes.node_host_installer_registry_units.test_host_drift_owner_dispatch_unit import (
    UNIT,
)
from tests.nodes.node_host_installer_registry_units.test_host_drift_owner_dispatch_unit import (
    checkout as drift_checkout,
)
from tests.nodes.node_host_installer_registry_units.test_host_drift_owner_dispatch_unit import (
    request as drift_request,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_drift_owner_unit_declares_kill_mode_process() -> None:
    unit = load_units()[UNIT]
    assert unit["kill_mode"] == "process"


def test_rendering_drift_owner_unit_includes_kill_mode_line(
    tmp_path: Path,
) -> None:
    revision = drift_checkout(tmp_path / "branch")
    req = drift_request(tmp_path, tmp_path / "branch", revision)
    service, _timer = HandlerRegistryUnits(base.Recorder()).render_systemd(
        req,
        load_units()[UNIT],
        tmp_path / "omni",
        "/usr/bin/uv",
        base.LINUX_PYTHON,
    )
    assert "KillMode=process" in service


def test_rendering_without_kill_mode_omits_kill_mode_line(
    tmp_path: Path,
) -> None:
    revision = drift_checkout(tmp_path / "branch")
    req = drift_request(tmp_path, tmp_path / "branch", revision)
    unit = load_units()[UNIT]
    service, _timer = HandlerRegistryUnits(base.Recorder()).render_systemd(
        req,
        unit | {"kill_mode": ""},
        tmp_path / "omni",
        "/usr/bin/uv",
        base.LINUX_PYTHON,
    )
    assert "KillMode" not in service

    service2, _timer2 = HandlerRegistryUnits(base.Recorder()).render_systemd(
        base.request(tmp_path, unit="overlay-store-load-systemd", path_overrides={}),
        load_units()["overlay-store-load-systemd"],
        tmp_path / "omni",
        "/usr/bin/uv",
        base.LINUX_PYTHON,
    )
    assert "KillMode" not in service2


# Systemd units declaring no kill_mode that render from the plain test request (a unit that names an input the
# request does not carry, such as the marketplace root, is rendered by its own unit tests).
UNDECLARED_UNITS = (
    "capture-silence-watch",
    "overlay-store-load-systemd",
    "memory-pilot-daily-systemd",
    "pilot-measurement-hourly-systemd",
    "scenario-r2-systemd",
    "scenario-r3-systemd",
    "scenario-r4-systemd",
    "scenario-r5-systemd",
    "scenario-r6-systemd",
    "scenario-r7-systemd",
    "scenario-r8-systemd",
    "scenario-r9-systemd",
    "lane-host-pool-snapshot-systemd",
    "lab-disk-hygiene-systemd",
    "stability-refresh-serve-systemd",
)


@pytest.mark.parametrize("unit_name", UNDECLARED_UNITS)
def test_a_unit_declaring_no_kill_mode_renders_the_service_file_it_did_before(
    tmp_path: Path, unit_name: str
) -> None:
    """Golden: the placeholder is appended to the last line, so an undeclared unit's text is unchanged."""
    unit = load_units()[unit_name]
    assert "kill_mode" not in unit
    service, _timer = HandlerRegistryUnits(base.Recorder()).render_systemd(
        base.request(tmp_path, unit=unit_name, path_overrides={}),
        unit,
        tmp_path / "omni",
        "/usr/bin/uv",
        base.LINUX_PYTHON,
    )
    assert "__KILL_MODE__" not in service
    assert "KillMode" not in service
    assert service.endswith(".err.log\n")


def test_the_kill_mode_line_is_the_only_difference_it_makes(tmp_path: Path) -> None:
    revision = drift_checkout(tmp_path / "branch")
    req = drift_request(tmp_path, tmp_path / "branch", revision)
    handler = HandlerRegistryUnits(base.Recorder())
    unit = load_units()[UNIT]
    declared, _ = handler.render_systemd(
        req, unit, tmp_path / "omni", "/usr/bin/uv", base.LINUX_PYTHON
    )
    undeclared, _ = handler.render_systemd(
        req,
        unit | {"kill_mode": ""},
        tmp_path / "omni",
        "/usr/bin/uv",
        base.LINUX_PYTHON,
    )
    assert declared == undeclared.rstrip("\n") + "\nKillMode=process\n"


def test_invalid_kill_mode_raises_value_error(
    tmp_path: Path,
) -> None:
    revision = drift_checkout(tmp_path / "branch")
    req = drift_request(tmp_path, tmp_path / "branch", revision)
    unit = load_units()[UNIT]
    with pytest.raises(ValueError, match="kill_mode"):
        HandlerRegistryUnits(base.Recorder()).render_systemd(
            req,
            unit | {"kill_mode": "none"},
            tmp_path / "omni",
            "/usr/bin/uv",
            base.LINUX_PYTHON,
        )


def test_launchd_unit_declaring_kill_mode_is_refused(
    tmp_path: Path,
) -> None:
    unit = load_units()["overlay-store-refresh"]
    with pytest.raises(ValueError, match="launchd units do not support"):
        HandlerRegistryUnits(base.Recorder()).render(
            base.request(tmp_path, unit="overlay-store-refresh"),
            unit | {"kill_mode": "process"},
            tmp_path / "omni",
            "/usr/bin/uv",
            base.LINUX_PYTHON,
        )


def test_seam_the_lane_runner_the_node_starts_outlives_the_unit_only_while_the_unit_declares_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seam, installer unit to remote-lane runner: the node starts the runner in its own session, and the unit keeps
    it alive by declaring kill_mode process; either side changing shape turns this red."""
    from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters import (
        runtime_adapters,
    )

    started: list[dict[str, object]] = []

    class Runner:
        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired("runner", timeout)

    def popen(argv: list[str], **kwargs: object) -> Runner:
        started.append(kwargs)
        return Runner()

    monkeypatch.setattr(runtime_adapters.subprocess, "Popen", popen)
    outcome = runtime_adapters.SubprocessLanePlacer(1.0, tmp_path).place(
        ("runner", "run", "--lane", "lane-x")
    )
    assert outcome.started
    assert started[0]["start_new_session"] is True
    assert load_units()[UNIT]["kill_mode"] == "process"


def test_seam_the_unit_names_the_variables_the_overlay_reads_the_runner_and_claude_directory_from() -> (
    None
):
    unit = load_units()[UNIT]
    assert unit["environment_inputs"]["ONEX_HOST_DRIFT_OWNER_RUNNER"]
    assert "ONEX_HOST_DRIFT_OWNER_CLAUDE_DIR" in unit["environment"]
