# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the daily memory pilot run is a declared systemd unit (OMN-23619).

The pilot runs once a day and writes the result file the pilot measurement node reads. The pilot's runtime module
(runtime_memory_pilot_run) is that ticket's deliverable, so the checkout this test renders from plants the entry
file, the way the overlay-store-load test builds its loader checkout.
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

UNIT = "memory-pilot-daily-systemd"
LABEL = "onex-memory-pilot-daily"
MODULE = "omnimarket.nodes.node_memory_pilot_effect.runtime_memory_pilot_run"
ENTRY = "src/omnimarket/nodes/node_memory_pilot_effect/runtime_memory_pilot_run.py"

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
    # make_checkout writes base.ENTRY; the pilot run's entry file is added and committed over it
    base.make_checkout(path)
    (path / ENTRY).parent.mkdir(parents=True, exist_ok=True)
    (path / ENTRY).write_text("print('pilot')\n", encoding="utf-8")
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


def test_the_contract_declares_the_daily_pilot_run_on_systemd() -> None:
    unit = load_units()[UNIT]
    assert unit["platform"] == "systemd"
    assert unit["kind"] == "registry_module_tick"
    assert unit["label"] == LABEL
    assert unit["schedule"]["start_interval"] == 86400
    assert unit["schedule"]["run_timeout_s"] == 10800
    assert unit["schedule"]["run_at_load"] is False
    assert unit["probe_argv"] == ["--probe"]
    assert unit["tick_no_sync"] is True
    assert unit["module"] == MODULE
    assert unit["entry_file"] == ENTRY
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"] == (
        "{registry_root}/overlays/overlays"
    )
    assert unit["environment"]["ONEX_STATE_DIR"] == "{registry_root}/.onex_state"
    node_dir = base.ROOT / "src/omnimarket/nodes/node_host_installer_registry_units"
    assert (node_dir / str(unit["template"])).is_file()
    assert (node_dir / str(unit["timer_template"])).is_file()


def test_dry_run_renders_a_daily_timer_running_the_pilot_module(tmp_path: Path) -> None:
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
        )
    )
    assert result.status == "success", result.stderr
    assert result.revision == revision
    assert not result.project_is_canonical
    exec_line = next(
        line for line in result.rendered.splitlines() if line.startswith("ExecStart=")
    )
    assert f'"-m" "{MODULE}"' in exec_line
    assert '"--no-sync"' in exec_line
    assert f'"--project" "{path}"' in exec_line
    assert "TimeoutStartSec=10800" in result.rendered
    # Once a day: OnUnitActiveSec=86400, and run_at_load false renders OnActiveSec=86400, not a fire at load.
    assert "OnUnitActiveSec=86400" in result.rendered
    assert "OnActiveSec=86400" in result.rendered
    assert f"Unit={LABEL}.service" in result.rendered
    assert (
        f'"ONEX_SKILL_OVERLAY_ROOTS={tmp_path}/omni/overlays/overlays"'
        in result.rendered
    )
    assert f'"ONEX_STATE_DIR={tmp_path}/omni/.onex_state"' in result.rendered
    assert "__" not in result.rendered.replace("__init__", "")
    # A dry-run writes nothing: neither the service nor the timer is on disk.
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    assert not (unit_dir / f"{LABEL}.service").exists()
    assert not (unit_dir / f"{LABEL}.timer").exists()
