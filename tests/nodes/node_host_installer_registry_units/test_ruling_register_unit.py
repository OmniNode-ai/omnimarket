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
import yaml

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_ruling_register_effect.overlay import load_route
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


def _rendered_dry_run(tmp_path: Path) -> tuple[str, Path]:
    """The dry run's rendered service and timer text, and the home the install named."""
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays"),
                "LAB_WORK_HOST_NAME": "h053",
            },
        )
    )
    assert result.status == "success", result.stderr
    return result.rendered, tmp_path / "home"


def test_the_rendered_unit_carries_the_whole_register_environment_the_limit_and_run_at_load(
    tmp_path: Path,
) -> None:
    rendered, home = _rendered_dry_run(tmp_path)
    overlays = tmp_path / "overlays" / "overlays"
    for assignment in (
        f"ONEX_PR_WATCHER_STATE={home}/.local/state/onex/pr-watcher-readonly/state.json",
        f"ONEX_STATE_DIR={tmp_path}/omni/.onex_state",
        f"ONEX_SKILL_OVERLAY_ROOTS={overlays}",
        f"ONEX_RULING_REGISTER_JOURNAL={tmp_path}/omni/.onex_state/ruling-register/journal.jsonl",
        "LAB_WORK_HOST_NAME=h053",
        f"OMNIBASE_PATH={tmp_path}/omni",
        f"ONEX_WORKSPACE_CONFIG_ROOT={tmp_path}/workspace-config",
    ):
        assert f'"{assignment}"' in rendered, assignment
    # The 600 s run limit (the register's 16 rows at the 90 s route timeout end in two waves, 180 s) and run-at-load.
    assert "TimeoutStartSec=600" in rendered
    assert (
        "OnActiveSec=1s" in rendered
    )  # run at load: the first reading follows the install by a second
    assert "OnUnitActiveSec=3600" in rendered
    assert "UnsetEnvironment=PYTHONPATH" in rendered


def test_an_install_runs_the_probe_with_the_rendered_environment_before_it_loads_the_unit(
    tmp_path: Path,
) -> None:
    path, revision = _checkout(tmp_path)
    run = base.Recorder()
    result = HandlerRegistryUnits(run).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    probe = next(call for call in run.calls if "--probe" in call)
    assert probe[0] == "env"
    assert MODULE in probe
    assert "LAB_WORK_HOST_NAME=h051" in probe
    # The probe is the module's last argument: it reads nothing and writes nothing.
    assert probe[-1] == "--probe"
    first_systemctl = next(call for call in run.calls if call[0] == "systemctl")
    assert run.calls.index(probe) < run.calls.index(first_systemctl)


def test_the_rendered_state_dir_plus_last_run_json_is_the_declared_evidence_file(
    tmp_path: Path,
) -> None:
    rendered, home = _rendered_dry_run(tmp_path)
    exec_line = next(
        line for line in rendered.splitlines() if line.startswith("ExecStart=")
    )
    words = [word.strip('"') for word in exec_line.removeprefix("ExecStart=").split()]
    state_dir = words[words.index("--state-dir") + 1]
    evidence_file = load_units()[UNIT]["evidence_file"]
    # The tick writes <--state-dir>/last-run.json and the unit's heartbeat reads <home>/<evidence_file>.
    assert Path(state_dir) / "last-run.json" == home / str(evidence_file)
    assert Path(str(evidence_file)).name == "last-run.json"


def test_an_install_that_names_no_host_is_refused_and_never_renders_an_unfilled_host_name(
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
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays")
            },
        )
    )
    assert result.status == "failure"
    assert "LAB_WORK_HOST_NAME" in result.stderr
    assert "{host_name}" in result.stderr
    assert "{host_name}" not in result.rendered


def test_the_unit_names_the_journal_the_route_overlay_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.node_overlay_support import install_node_overlay

    example = yaml.safe_load(
        (
            base.ROOT
            / "src/omnimarket/nodes/node_ruling_register_effect/overlay.example.yaml"
        ).read_text(encoding="utf-8")
    )
    install_node_overlay(monkeypatch, tmp_path, "node_ruling_register_effect", example)
    journal_env = load_route({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}).journal_env
    # If the unit set another name the journal would silently stay in memory (the sink is built from the route's name).
    assert journal_env in load_units()[UNIT]["environment"]


def test_the_ac4_check_reads_the_installed_status_on_h201_and_asserts_the_head_and_the_age() -> (
    None
):
    contract = yaml.safe_load(
        (base.ROOT / "contracts/OMN-24944.yaml").read_text(encoding="utf-8")
    )
    entry = next(item for item in contract["dod_evidence"] if "AC4" in item["binds_ac"])
    assert entry["execution_scope"] == "local_done_gate"
    command = "\n".join(str(check["check_value"]) for check in entry["checks"])
    # Rerunning the unit tests passes whether or not the unit is installed; the check reads the install itself.
    assert "-m pytest" not in command
    assert "uv run" not in command
    assert "onex-host-install-units ruling-register-systemd --status" in command
    assert "h201" in command
    # The head: the installer's recorded revision is compared with the checked-out head.
    assert "git rev-parse HEAD" in command
    assert "installed revision" in command
    # The age: last-run.json younger than 7200 s (two intervals of the 3600 s timer).
    assert "7200" in command
    assert "s old" in command
    assert load_units()[UNIT]["schedule"]["start_interval"] * 2 == 7200
