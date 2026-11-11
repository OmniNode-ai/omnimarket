# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the hourly pilot measurement reading is a declared systemd unit (OMN-23619).

The reading is the pilot measurement node's own declared schedule (its contract's ``schedule.unit_name``); this unit
installs it through the host installer so it may run from a branch checkout. The test checks the unit against the node's
contract (the seam), renders it, and shows the install refused without the host name its row needs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_pilot_measurement_effect.handlers.schedule import (
    load_schedule,
)
from tests.nodes.node_host_installer_registry_units import (
    test_memory_pilot_daily_unit as memory_unit,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

UNIT = "pilot-measurement-hourly-systemd"
MODULE = "omnimarket.nodes.node_pilot_measurement_effect.runtime_pilot_measurement_tick"
ENTRY = "src/omnimarket/nodes/node_pilot_measurement_effect/runtime_pilot_measurement_tick.py"

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
    (path / ENTRY).write_text("print('measurement')\n", encoding="utf-8")
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


def test_the_unit_is_the_nodes_own_declared_schedule() -> None:
    unit = load_units()[UNIT]
    schedule = load_schedule()
    # The seam with the node's contract and the automation liveness row: the same unit name, an hourly interval.
    assert unit["label"] == schedule.unit_name
    assert unit["schedule"]["start_interval"] == 3600
    assert unit["schedule"]["run_timeout_s"] == schedule.timeout_seconds
    assert unit["module"] == MODULE
    assert unit["entry_file"] == ENTRY
    assert unit["probe_argv"] == ["--probe"]
    assert unit["required_environment_inputs"] == ["ONEX_LEDGER_HOST_NAME"]
    node_dir = base.ROOT / "src/omnimarket/nodes/node_host_installer_registry_units"
    assert (node_dir / str(unit["template"])).is_file()
    assert (node_dir / str(unit["timer_template"])).is_file()
    assert (base.ROOT / ENTRY).is_file()


def test_dry_run_renders_an_hourly_timer_with_the_reading_environment(
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
            ledger_host_name="h053",
        )
    )
    assert result.status == "success", result.stderr
    exec_line = next(
        line for line in result.rendered.splitlines() if line.startswith("ExecStart=")
    )
    assert f'"-m" "{MODULE}"' in exec_line
    assert f'"--project" "{path}"' in exec_line
    assert "OnUnitActiveSec=3600" in result.rendered
    assert f"Unit={load_schedule().unit_name}.service" in result.rendered
    assert '"ONEX_LEDGER_HOST_NAME=h053"' in result.rendered
    assert (
        f'"ONEX_MEMORY_PILOT_RESULT_FILE={tmp_path}/omni/.onex_state/memory-pilot-daily/result.json"'
        in result.rendered
    )
    assert "__" not in result.rendered.replace("__init__", "")


def test_the_install_is_refused_without_the_host_name_the_row_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control: with no host name the unit would end INDETERMINATE every tick, so it must not install."""
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
        )
    )
    assert result.status != "success"
    assert "ONEX_LEDGER_HOST_NAME" in result.stderr


def test_the_memory_unit_still_feeds_the_file_this_unit_reads() -> None:
    """Seam: the measurement unit reads the result file path the pilot run's unit writes it to."""
    read = load_units()[UNIT]["environment"]["ONEX_MEMORY_PILOT_RESULT_FILE"]
    assert read == "{registry_root}/.onex_state/memory-pilot-daily/result.json"
    assert (
        load_units()[memory_unit.UNIT]["state_directory"]
        == ".onex_state/memory-pilot-daily"
    )
