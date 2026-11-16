# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the overlay store loader is a declared hourly systemd unit (OMN-23353).

Until this unit existed the loader on a pool host was a hand-started transient timer that loaded a stale branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

UNIT = "overlay-store-load-systemd"
LABEL = "onex-overlay-store-load"
ENTRY = "src/omnimarket/nodes/node_overlay_store_load_effect/runtime_overlay_store_load_tick.py"

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def _checkout(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "branch"
    # make_checkout writes base.ENTRY; the loader's tick entry file is added and committed over it
    base.make_checkout(path)
    (path / ENTRY).parent.mkdir(parents=True, exist_ok=True)
    (path / ENTRY).write_text("print('tick')\n", encoding="utf-8")
    base._git(["git", "-C", str(path), "add", "-A"], check=True)
    base._git(
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
            "y",
        ],
        check=True,
    )
    revision = base._git(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return path, revision


def test_the_unit_is_declared_hourly_on_systemd_and_reads_the_overlay_home_clone() -> (
    None
):
    unit = load_units()[UNIT]
    assert unit["platform"] == "systemd"
    assert unit["schedule"]["start_interval"] == 3600
    assert unit["module"].endswith("runtime_overlay_store_load_tick")
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"] == ("{overlays_root}")


def test_install_renders_an_hourly_timer_running_the_tick_over_the_clone_overlay_root(
    tmp_path: Path,
) -> None:
    path, revision = _checkout(tmp_path)
    run = base.Recorder()
    request = base.request(
        tmp_path,
        unit=UNIT,
        mode="install",
        project_path=str(path),
        expected_revision=revision,
        path_overrides={},
        workspace_config_root=str(tmp_path / "config-root"),
    )
    result = HandlerRegistryUnits(run).handle(request)
    assert result.status == "success", result.stderr
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    service = (unit_dir / f"{LABEL}.service").read_text(encoding="utf-8")
    timer = (unit_dir / f"{LABEL}.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=3600" in timer
    assert (
        '"-m" "omnimarket.nodes.node_overlay_store_load_effect.runtime_overlay_store_load_tick"'
        in service
    )
    assert f'"ONEX_SKILL_OVERLAY_ROOTS={tmp_path}/omni/overlays/overlays"' in service
    assert "TimeoutStartSec=120" in service
    assert run.systemctl() == [
        ["daemon-reload"],
        ["enable", f"{LABEL}.timer"],
        ["restart", f"{LABEL}.timer"],
    ]


def test_a_checkout_without_the_tick_entry_file_is_refused(tmp_path: Path) -> None:
    """Error chain: the installer refuses a branch that does not carry the loader's tick."""
    path = tmp_path / "branch"
    base.make_checkout(path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            path_overrides={},
        )
    )
    assert result.status == "failure"
    assert not (tmp_path / "home/.config/systemd/user" / f"{LABEL}.timer").exists()


@pytest.mark.parametrize(
    "name",
    [
        "overlay-store-load-systemd",
        "overlay-store-refresh-systemd",
        "overlay-store-refresh",
    ],
)
def test_the_store_units_find_the_lane_declaration_from_the_workspace_root(
    name: str,
) -> None:
    """The overlays name a bus lane, not a broker: the lane declaration is read from the workspace root (OMN-24052).

    Measured on h053 after the overlays moved to the dev lane: the loader unit exited 69 with "no workspace root is
    set" until the unit carried OMNIBASE_PATH.
    """
    environment = load_units()[name]["environment"]
    assert environment["OMNIBASE_PATH"] == "{registry_root}"
    assert environment["ONEX_WORKSPACE_CONFIG_ROOT"] == "{workspace_config_root}"
