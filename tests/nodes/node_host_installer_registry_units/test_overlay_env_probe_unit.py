# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay environment probe has a caller: a declared systemd unit on a lab host (OMN-26549).

The probe node (``node_host_installer_overlay_env_probe``) declares direct invocation by the host installer as its
only trigger. These tests read the real unit registry, so a probe that no declared unit invokes fails here.
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
from tests.nodes.node_host_installer_registry_units.test_registry_units_systemd import (
    HANDLER,
    LINUX_PYTHON,
    Recorder,
    _git,
)

PROBE_PACKAGE = "omnimarket.nodes.node_host_installer_overlay_env_probe"
TICK_MODULE = f"{PROBE_PACKAGE}.runtime_overlay_env_probe_tick"
ENTRY = "src/omnimarket/nodes/node_host_installer_overlay_env_probe/runtime_overlay_env_probe_tick.py"


def _probe_units() -> dict[str, dict[str, object]]:
    return {
        name: declared
        for name, declared in load_units().items()
        if str(declared.get("module", "")).startswith(PROBE_PACKAGE)
    }


def test_a_declared_unit_invokes_the_overlay_env_probe() -> None:
    units = _probe_units()
    assert units, (
        "no unit in the registry invokes node_host_installer_overlay_env_probe"
    )
    for name, declared in units.items():
        assert declared["module"] == TICK_MODULE, name
        assert declared["platform"] == "systemd", name
        assert declared["entry_file"] == ENTRY, name


def test_the_probe_unit_ticks_inside_its_run_limit() -> None:
    (declared,) = _probe_units().values()
    schedule = declared["schedule"]
    assert isinstance(schedule, dict)
    assert 0 < schedule["run_timeout_s"] <= schedule["start_interval"]


def test_the_probe_unit_names_the_host_and_the_overlay_roots() -> None:
    (declared,) = _probe_units().values()
    environment = declared["environment"]
    assert isinstance(environment, dict)
    assert environment["LAB_WORK_HOST_NAME"] == "{host_name}"
    assert environment["ONEX_SKILL_OVERLAY_ROOTS"].startswith("{overlays_root}")
    refused = declared["environment_refuse_worktree_paths"]
    assert isinstance(refused, list)
    assert "ONEX_SKILL_OVERLAY_ROOTS" in refused


@pytest.fixture
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def _checkout(path: Path) -> str:
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


def test_the_rendered_service_runs_the_probe_and_the_timer_ticks(
    tmp_path: Path, linux_host: None
) -> None:
    (unit,) = _probe_units()
    path = tmp_path / "branch"
    revision = _checkout(path)
    result = HandlerRegistryUnits(Recorder()).handle(
        ModelRegistryUnitsRequest.model_validate(
            {
                "unit": unit,
                "home": str(tmp_path / "home"),
                "registry_root": str(tmp_path / "omni"),
                "project_path": str(path),
                "expected_revision": revision,
                "path_overrides": {
                    "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays"),
                    "LAB_WORK_HOST_NAME": "h053",
                },
            }
        )
    )
    assert result.status == "success", result.stderr
    assert f'"-m" "{TICK_MODULE}"' in result.rendered
    assert '"LAB_WORK_HOST_NAME=h053"' in result.rendered
    assert "OnUnitActiveSec=" in result.rendered


def test_the_probe_contract_names_the_unit_that_runs_it() -> None:
    import yaml

    from omnimarket.nodes.node_host_installer_overlay_env_probe import (
        node as probe_node,
    )

    contract = yaml.safe_load(
        probe_node.NodeHostInstallerOverlayEnvProbe.contract_path.read_text()
    )
    unit = contract["runtime_dispatch"]["host_unit"]["unit"]
    assert unit in _probe_units()
