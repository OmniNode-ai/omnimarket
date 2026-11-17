# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host table copies drift reading is installed as a daily systemd registry tick (OMN-25159)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_table_copies_drift_compute import (
    runtime_host_table_copies_drift_tick as tick,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

UNIT = "host-table-copies-drift-systemd"
MODULE = "omnimarket.nodes.node_host_table_copies_drift_compute.runtime_host_table_copies_drift_tick"
ENTRY = "src/omnimarket/nodes/node_host_table_copies_drift_compute/runtime_host_table_copies_drift_tick.py"

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
    base.make_checkout(path)
    (path / ENTRY).parent.mkdir(parents=True, exist_ok=True)
    (path / ENTRY).write_text("print('drift')\n", encoding="utf-8")
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


def test_the_unit_declares_the_daily_tick_seam() -> None:
    unit = load_units()[UNIT]
    assert unit["schedule"]["start_interval"] == 86400
    assert unit["module"] == MODULE
    assert unit["entry_file"] == ENTRY
    assert unit["probe_argv"] == ["--probe"]
    assert unit["required_environment_inputs"] == ["ONEX_LEDGER_HOST_NAME"]
    assert (
        unit["environment"]["ONEX_WORKSPACE_CONFIG_ROOT"] == "{workspace_config_root}"
    )
    node_dir = base.ROOT / "src/omnimarket/nodes/node_host_installer_registry_units"
    assert (node_dir / str(unit["template"])).is_file()
    assert (node_dir / str(unit["timer_template"])).is_file()
    assert (base.ROOT / ENTRY).is_file()


def test_dry_run_renders_the_daily_timer_and_reading_environment(
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
            path_overrides={},
            workspace_config_root=str(tmp_path / "config-root"),
            ledger_host_name="h053",
        )
    )
    assert result.status == "success", result.stderr
    assert "OnUnitActiveSec=86400" in result.rendered
    assert '"ONEX_LEDGER_HOST_NAME=h053"' in result.rendered
    assert f'"ONEX_WORKSPACE_CONFIG_ROOT={tmp_path}/config-root"' in result.rendered
    assert (
        f'"ONEX_LEDGER_BUS_APPEND_COMMAND={tmp_path}/omni/omnimarket/.venv/bin/python -m '
        "omnimarket.work_ledger_bus.cli append --bus-lane dev --omnibase-path "
        f'{tmp_path}/omni"'
    ) in result.rendered
    assert "__" not in result.rendered.replace("__init__", "")


def test_install_is_refused_without_the_required_ledger_host_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_HOST_NAME", raising=False)
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            workspace_config_root=str(tmp_path / "config-root"),
        )
    )
    assert result.status != "success"
    assert "ONEX_LEDGER_HOST_NAME" in result.stderr


def test_probe_without_overlay_roots_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    assert tick.main(["--probe"]) == 3
    assert "INDETERMINATE:" in capsys.readouterr().err


def test_probe_loads_the_overlay_without_reading_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    overlays = tmp_path / "overlays"
    target = overlays / "node_host_table_copies_drift_compute" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    shutil.copyfile(
        base.ROOT
        / "src/omnimarket/nodes/node_host_table_copies_drift_compute/overlay.example.yaml",
        target,
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlays))
    assert tick.main(["--probe"]) == 0
    assert "PROBE ok" in capsys.readouterr().out


def test_non_probe_delegates_to_the_daily_verb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def daily(argv: list[str]) -> int:
        calls.append(argv)
        return 7

    monkeypatch.setattr(tick, "drift_main", daily)
    assert tick.main([]) == 7
    assert calls == [["daily"]]
