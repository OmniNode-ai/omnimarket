# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: a systemd unit declares its service type (OMN-24901).

The template wrote Type=oneshot for every unit, because it was written for a timer-fired tick. The Temporal server and
worker are long-running: as oneshot units they sit in ActiveState=activating SubState=start for days and never read
active (running). A unit may now declare ``service_type: simple``: the service is then Type=simple with
Restart=on-failure and a RestartSec equal to the start interval the node's other long-running units use, and the
installer restarts the service itself when it installs a changed unit. A unit declaring nothing is a oneshot tick
exactly as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

pytestmark = pytest.mark.unit

LONG_RUNNING = ("temporal-server-systemd", "temporal-worker-systemd")
TICK = "temporal-health-alarm-systemd"
# The restart delay of the node's other long-running unit (a 60 s timer start within any exit).
OTHER_LONG_RUNNING = "stability-refresh-serve-systemd"


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def request(tmp_path: Path, unit: str, **kw: object) -> ModelRegistryUnitsRequest:
    offered = {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays"),
        "ONEX_TEMPORAL_WORKER_PROJECT": str(tmp_path / "worker"),
    }
    allowed = set(load_units()[unit].get("environment_path_overrides") or [])
    fields: dict[str, object] = {
        "unit": unit,
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "ledger_host_name": "h-example",
        "ledger_path": "/example/ledger/rows.md",
        "appender_root": "/example/internal",
        "path_overrides": {k: v for k, v in offered.items() if k in allowed},
    }
    fields.update(kw)
    return ModelRegistryUnitsRequest.model_validate(fields)


def render(tmp_path: Path, unit_name: str, **declared: object) -> str:
    unit = load_units()[unit_name] | declared
    service, _timer = HandlerRegistryUnits(base.Recorder()).render_systemd(
        request(tmp_path, unit_name),
        unit,
        tmp_path / "omni",
        "/usr/bin/uv",
        base.LINUX_PYTHON,
    )
    return service


@pytest.mark.parametrize("unit_name", LONG_RUNNING)
def test_a_long_running_temporal_unit_is_type_simple_with_restart_on_failure(
    tmp_path: Path, unit_name: str
) -> None:
    service = render(tmp_path, unit_name)
    lines = service.splitlines()
    assert "Type=simple" in lines
    assert "Type=oneshot" not in lines
    assert "Restart=on-failure" in lines


@pytest.mark.parametrize("unit_name", LONG_RUNNING)
def test_the_restart_delay_is_the_one_the_other_long_running_unit_uses(
    tmp_path: Path, unit_name: str
) -> None:
    delay = load_units()[OTHER_LONG_RUNNING]["schedule"]["start_interval"]
    assert f"RestartSec={delay}" in render(tmp_path, unit_name).splitlines()


def test_the_timer_fired_tick_stays_oneshot(tmp_path: Path) -> None:
    service = render(tmp_path, TICK)
    lines = service.splitlines()
    assert "Type=oneshot" in lines
    assert not [line for line in lines if line.startswith(("Restart", "Type=simple"))]


def test_a_unit_declaring_no_service_type_renders_a_oneshot(tmp_path: Path) -> None:
    unit = load_units()[OTHER_LONG_RUNNING]
    assert "service_type" not in unit
    service, _timer = HandlerRegistryUnits(base.Recorder()).render_systemd(
        base.request(tmp_path, unit=OTHER_LONG_RUNNING, path_overrides={}),
        unit,
        tmp_path / "omni",
        "/usr/bin/uv",
        base.LINUX_PYTHON,
    )
    assert "Type=oneshot" in service.splitlines()
    assert "Restart" not in service


def test_an_unknown_service_type_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="service_type"):
        render(tmp_path, LONG_RUNNING[0], service_type="forking")


def test_a_launchd_unit_declaring_a_service_type_is_refused(tmp_path: Path) -> None:
    unit = load_units()["overlay-store-refresh"]
    with pytest.raises(ValueError, match="launchd units do not support"):
        HandlerRegistryUnits(base.Recorder()).render(
            base.request(tmp_path, unit="overlay-store-refresh"),
            unit | {"service_type": "simple"},
            tmp_path / "omni",
            "/usr/bin/uv",
            base.LINUX_PYTHON,
        )


def test_installing_a_long_running_unit_restarts_its_service(
    tmp_path: Path,
) -> None:
    """A changed unit file does not reach a process that is already running; the installer restarts the service."""
    unit_name = LONG_RUNNING[0]
    entry = load_units()[unit_name]["entry_file"]
    checkout = tmp_path / "branch"
    checkout.mkdir()
    base._git(["git", "-C", str(checkout), "init", "-q", "-b", "branch"], check=True)
    (checkout / entry).parent.mkdir(parents=True)
    (checkout / entry).write_text("print('serve')\n", encoding="utf-8")
    base._git(["git", "-C", str(checkout), "add", "-A"], check=True)
    base._git(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "x",
        ],
        check=True,
    )
    revision = base._git(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    run = base.Recorder()
    result = HandlerRegistryUnits(run).handle(
        request(
            tmp_path,
            unit_name,
            mode="install",
            project_path=str(checkout),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    label = load_units()[unit_name]["label"]
    assert run.systemctl() == [
        ["daemon-reload"],
        ["enable", f"{label}.timer"],
        ["restart", f"{label}.timer"],
        ["restart", f"{label}.service"],
    ]
