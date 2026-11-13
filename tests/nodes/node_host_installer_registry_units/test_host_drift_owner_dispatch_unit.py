# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host drift owner step is a declared systemd unit that names its runner, so no one sets the dispatch command by hand (OMN-24239)."""

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

UNIT = "host-drift-owner-dispatch-systemd"
LABEL = "onex-host-drift-owner-dispatch"
ENTRY = "src/omnimarket/nodes/node_host_drift_owner_dispatch_effect/runtime_host_drift_owner_dispatch.py"


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
        "workspace_config_root": str(tmp_path / "ws"),
        "remote_lane_runner": "/example/plugin/remote-lane/onex_remote_lane.py",
        "ledger_host_name": "h-example",
        "ledger_path": "/example/ledger/rows.md",
        "project_path": str(path),
        "expected_revision": revision,
        "path_overrides": {
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays")
        },
    }
    fields.update(kw)
    return ModelRegistryUnitsRequest.model_validate(fields)


def test_the_unit_declares_a_run_limit_under_its_interval_and_the_nodes_timeout() -> (
    None
):
    declared = load_units()[UNIT]
    assert declared["platform"] == "systemd"
    assert (
        0
        < declared["schedule"]["run_timeout_s"]
        <= declared["schedule"]["start_interval"]
    )
    assert declared["schedule"]["run_timeout_s"] * 1000 <= 600000


def test_the_service_runs_the_owner_step_and_names_the_runner(tmp_path: Path) -> None:
    path = tmp_path / "branch"
    revision = checkout(path)
    result = HandlerRegistryUnits(Recorder()).handle(request(tmp_path, path, revision))
    assert result.status == "success", result.stderr
    assert (
        '"-m" "omnimarket.nodes.node_host_drift_owner_dispatch_effect.runtime_host_drift_owner_dispatch"'
        in result.rendered
    )
    runner = "/example/plugin/remote-lane/onex_remote_lane.py"
    assert f'"ONEX_HOST_DRIFT_OWNER_RUNNER={runner}"' in result.rendered
    assert "TimeoutStartSec=600" in result.rendered
    assert "OnUnitActiveSec=900" in result.rendered
    assert f"Unit={LABEL}.service" in result.rendered


def test_the_runner_variable_is_the_one_the_nodes_overlay_names() -> None:
    from omnimarket.nodes.node_host_drift_owner_dispatch_effect.overlay import NODE_NAME

    declared = load_units()[UNIT]
    assert NODE_NAME in declared["module"]
    assert (
        declared["environment_inputs"]["ONEX_HOST_DRIFT_OWNER_RUNNER"]
        == "remote_lane_runner"
    )
    assert len(declared["required_environment_inputs"]) == 3


def test_an_install_that_names_no_runner_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_HOST_DRIFT_OWNER_RUNNER", raising=False)
    path = tmp_path / "branch"
    revision = checkout(path)
    result = HandlerRegistryUnits(Recorder()).handle(
        request(tmp_path, path, revision, mode="install", remote_lane_runner="")
    )
    assert result.status == "failure"
    assert "ONEX_HOST_DRIFT_OWNER_RUNNER" in result.stderr


def test_the_runner_gets_the_ledger_write_environment_the_checkers_own_unit_has(
    tmp_path: Path,
) -> None:
    path = tmp_path / "branch"
    revision = checkout(path)
    result = HandlerRegistryUnits(Recorder()).handle(request(tmp_path, path, revision))
    assert result.status == "success", result.stderr
    assert '"ONEX_LEDGER_WRITE_VIA=bus"' in result.rendered
    assert '"ONEX_LEDGER_HOST_NAME=h-example"' in result.rendered
    assert "=/example/ledger/rows.md" in result.rendered
    assert "omnimarket.work_ledger_bus.cli append --bus-lane dev" in result.rendered
