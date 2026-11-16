# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane closeout sweep is a declared systemd unit on every lab host (OMN-23375)."""

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
from tests.nodes.node_host_installer_registry_units.test_registry_units_systemd import (
    HANDLER,
    LINUX_PYTHON,
    Recorder,
    _git,
)

UNIT = "lane-closeout-sweep-systemd"
LABEL = "onex-lane-closeout-sweep"
ENTRY = "src/omnimarket/nodes/node_lane_closeout_sweep_effect/runtime_lane_closeout_sweep.py"


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def checkout(path: Path) -> str:
    path.mkdir(parents=True)
    _git(["git", "-C", str(path), "init", "-q", "-b", "branch"], check=True)
    (path / ENTRY).parent.mkdir(parents=True)
    (path / ENTRY).write_text("print('tick')\n", encoding="utf-8")
    _git(["git", "-C", str(path), "add", "-A"], check=True)
    _git(
        [
            "git",
            "-C",
            str(path),
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
    return _git(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def request(
    tmp_path: Path, path: Path, revision: str, **kw: object
) -> ModelRegistryUnitsRequest:
    fields: dict[str, object] = {
        "unit": UNIT,
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "project_path": str(path),
        "expected_revision": revision,
        "path_overrides": {
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays"),
            "LAB_WORK_HOST_NAME": "h053",
        },
    }
    fields.update(kw)
    return ModelRegistryUnitsRequest.model_validate(fields)


def test_the_unit_declares_a_run_limit_under_its_interval() -> None:
    declared = load_units()[UNIT]
    assert (
        0
        < declared["schedule"]["run_timeout_s"]
        <= declared["schedule"]["start_interval"]
    )
    assert declared["platform"] == "systemd"


def test_the_service_runs_the_sweep_and_the_timer_ticks(tmp_path: Path) -> None:
    path = tmp_path / "branch"
    revision = checkout(path)
    result = HandlerRegistryUnits(Recorder()).handle(request(tmp_path, path, revision))
    assert result.status == "success", result.stderr
    assert (
        '"-m" "omnimarket.nodes.node_lane_closeout_sweep_effect.runtime_lane_closeout_sweep"'
        in result.rendered
    )
    assert '"LAB_WORK_HOST_NAME=h053"' in result.rendered
    assert "TimeoutStartSec=240" in result.rendered
    assert "OnUnitActiveSec=300" in result.rendered
    assert f"Unit={LABEL}.service" in result.rendered


def test_install_enables_the_timer(tmp_path: Path) -> None:
    path = tmp_path / "branch"
    revision = checkout(path)
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(
        request(tmp_path, path, revision, mode="install")
    )
    assert result.status == "success", result.stderr
    assert run.systemctl() == [
        ["daemon-reload"],
        ["enable", f"{LABEL}.timer"],
        ["restart", f"{LABEL}.timer"],
    ]
