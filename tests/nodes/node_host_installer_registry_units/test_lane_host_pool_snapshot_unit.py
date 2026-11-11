# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the lane host pool snapshot is a declared 60-second systemd unit (OMN-23604).

Until this unit existed the snapshot on a pool host was a hand-started transient timer that read a stale branch.
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

UNIT = "lane-host-pool-snapshot-systemd"
LABEL = "onex-lane-host-pool-snapshot"
ENTRY = "src/omnimarket/nodes/node_lane_host_pool_snapshot_effect/__main__.py"

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
    # make_checkout writes base.ENTRY; the snapshot's entry file is added and committed over it
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


def test_the_unit_is_declared_on_systemd_and_reads_the_overlay_home_clone() -> None:
    unit = load_units()[UNIT]
    assert unit["platform"] == "systemd"
    assert unit["schedule"]["start_interval"] == 60
    assert unit["module"] == "omnimarket.nodes.node_lane_host_pool_snapshot_effect"
    assert unit["entry_file"] == ENTRY
    assert unit["schedule"]["run_timeout_s"] < 60
    assert unit["probe_argv"] == ["--probe"]
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"] == (
        "{registry_root}/overlays/overlays"
    )
    assert unit["environment"]["OMNIBASE_PATH"] == "{registry_root}"
    assert unit["environment"]["PATH"].startswith(
        "{registry_root}/omnibase_infra/scripts"
    )


def test_install_renders_a_60_second_timer_running_the_module_from_the_branch_checkout(
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
        workspace_config_root=str(tmp_path / "config"),
        path_overrides={},
    )
    result = HandlerRegistryUnits(run).handle(request)
    assert result.status == "success", result.stderr
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    service = (unit_dir / f"{LABEL}.service").read_text(encoding="utf-8")
    timer = (unit_dir / f"{LABEL}.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=60" in timer
    assert '"-m" "omnimarket.nodes.node_lane_host_pool_snapshot_effect"' in service
    assert f'"ONEX_SKILL_OVERLAY_ROOTS={tmp_path}/omni/overlays/overlays"' in service
    assert f'"OMNIBASE_PATH={tmp_path}/omni"' in service
    assert f'"PATH={tmp_path}/omni/omnibase_infra/scripts' in service
    assert "TimeoutStartSec=50" in service
    assert run.systemctl() == [
        ["daemon-reload"],
        ["enable", f"{LABEL}.timer"],
        ["restart", f"{LABEL}.timer"],
    ]


def test_a_checkout_without_the_entry_file_is_refused(tmp_path: Path) -> None:
    """Error chain: the installer refuses a branch that does not carry the snapshot's entry file."""
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
