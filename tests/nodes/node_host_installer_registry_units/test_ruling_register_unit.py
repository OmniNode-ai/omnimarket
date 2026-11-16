# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the standing-rulings register reading is a declared systemd unit (OMN-24944).

The register's rows read over ssh, the ledger bus and the repository clones, which only a pool host holds, so the
reading runs as an hourly timer installed through the host installer. The test checks the unit against the tick
module it runs (the seam) and renders it.
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

UNIT = "ruling-register-systemd"
MODULE = "omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register_tick"
ENTRY = (
    "src/omnimarket/nodes/node_ruling_register_effect/runtime_ruling_register_tick.py"
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


def _checkout(tmp_path: Path) -> tuple[Path, str]:
    """A clean branch checkout that carries the unit's entry file, and its revision."""
    path = tmp_path / "branch"
    base.make_checkout(path)
    (path / ENTRY).parent.mkdir(parents=True, exist_ok=True)
    (path / ENTRY).write_text("print('register')\n", encoding="utf-8")
    base._git(["git", "-C", str(path), "add", "-A"], check=True)
    commit = [
        "git",
        "-C",
        str(path),
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
    ]
    base._git([*commit, "commit", "-q", "-m", "y"], check=True)
    revision = base._git(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return path, revision


def test_the_unit_is_declared_with_an_hourly_timer_the_probe_and_the_ledger_reading_environment() -> (
    None
):
    unit = load_units()[UNIT]
    assert unit["platform"] == "systemd"
    assert unit["label"] == "onex-ruling-register"
    assert unit["schedule"]["start_interval"] == 3600
    assert unit["module"] == MODULE
    assert unit["entry_file"] == ENTRY
    assert unit["probe_argv"] == ["--probe"]
    # The rows that read the ledger over the bus find the lane declaration table and the lane under these.
    assert set(unit["environment"]) >= {
        "ONEX_SKILL_OVERLAY_ROOTS",
        "LAB_WORK_HOST_NAME",
        "OMNIBASE_PATH",
        "ONEX_WORKSPACE_CONFIG_ROOT",
        "ONEX_PR_WATCHER_STATE",
        "ONEX_RULING_REGISTER_JOURNAL",
    }
    assert "LAB_WORK_HOST_NAME" in unit["environment_path_overrides"]
    node_dir = base.ROOT / "src/omnimarket/nodes/node_host_installer_registry_units"
    assert (node_dir / str(unit["template"])).is_file()
    assert (node_dir / str(unit["timer_template"])).is_file()
    assert (base.ROOT / ENTRY).is_file()


def test_dry_run_renders_an_hourly_timer_that_runs_the_tick_under_the_host_name(
    tmp_path: Path,
) -> None:
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(path),
            expected_revision=revision,
            # The install names the host it runs on: LAB_WORK_HOST_NAME has no default, as in the store units.
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays"),
                "LAB_WORK_HOST_NAME": "h053",
            },
        )
    )
    assert result.status == "success", result.stderr
    exec_line = next(
        line for line in result.rendered.splitlines() if line.startswith("ExecStart=")
    )
    assert f'"-m" "{MODULE}"' in exec_line
    assert f'"--project" "{path}"' in exec_line
    assert "OnUnitActiveSec=3600" in result.rendered
    assert '"LAB_WORK_HOST_NAME=h053"' in result.rendered
    assert f'"OMNIBASE_PATH={tmp_path}/omni"' in result.rendered
    assert (
        f'"ONEX_WORKSPACE_CONFIG_ROOT={tmp_path}/workspace-config"' in result.rendered
    )
    assert (
        f'"ONEX_RULING_REGISTER_JOURNAL={tmp_path}/omni/.onex_state/ruling-register/journal.jsonl"'
        in result.rendered
    )
    assert "__" not in result.rendered.replace("__init__", "")
