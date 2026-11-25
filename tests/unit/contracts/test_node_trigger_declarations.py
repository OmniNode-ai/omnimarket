# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census, the lane dispatcher and the dev identity provisioner each declare what runs them.

The census and the dispatcher are run by a timer unit of node_host_installer_registry_units; the provisioner writes a user and
a tenant, so it is run on demand by a named command with a named owner. The declarations are typed
(omnimarket.models.model_node_trigger), and each is checked against the thing it names.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from pydantic import ValidationError

from omnimarket.models.model_node_trigger import (
    ModelOnDemandTrigger,
    ModelRuntimeDispatchTriggers,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
NODES = REPO_ROOT / "src" / "omnimarket" / "nodes"
CENSUS = "node_work_ledger_file_census_effect"
DISPATCH = "node_lane_dispatch_intake_effect"
IDENTITY = "node_dev_identity_provision_effect"
CENSUS_UNIT = "ledger-file-census-systemd"
DAY_SECONDS = 86400


def _dispatch_block(node: str) -> dict[str, Any]:
    contract = yaml.safe_load((NODES / node / "contract.yaml").read_text("utf-8"))
    return dict(contract["runtime_dispatch"])


def _triggers(node: str) -> ModelRuntimeDispatchTriggers:
    return ModelRuntimeDispatchTriggers.model_validate(_dispatch_block(node))


@pytest.fixture
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_the_census_declares_a_daily_host_unit_that_runs_its_tick_module() -> None:
    trigger = _triggers(CENSUS).host_unit
    assert trigger is not None
    assert trigger.unit == CENSUS_UNIT
    units = load_units()
    assert trigger.runs_node(units, CENSUS)
    unit = units[CENSUS_UNIT]
    assert unit["platform"] == "systemd"
    assert unit["schedule"]["start_interval"] == DAY_SECONDS
    assert unit["schedule"]["run_timeout_s"] < DAY_SECONDS
    assert (REPO_ROOT / unit["entry_file"]).is_file()
    assert unit["probe_argv"] == ["--probe"]
    assert unit["environment"]["ONEX_LEDGER_CENSUS_ROOT"] == "{registry_root}"


def test_the_census_unit_renders_a_daily_timer_that_runs_the_tick_module(
    tmp_path: Path, linux_host: None
) -> None:
    """The installer turns the declared schedule into the timer that runs the node once a day."""
    unit = load_units()[CENSUS_UNIT]
    checkout = tmp_path / "branch"
    base.make_checkout(checkout)
    (checkout / unit["entry_file"]).parent.mkdir(parents=True, exist_ok=True)
    (checkout / unit["entry_file"]).write_text("print('tick')\n", encoding="utf-8")
    base._git(["git", "-C", str(checkout), "add", "-A"], check=True)
    base._git(
        [
            *("git", "-C", str(checkout), "-c", "user.name=t"),
            *("-c", "user.email=t@e.com", "commit", "-q", "-m", "y"),
        ],
        check=True,
    )
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=CENSUS_UNIT,
            mode="install",
            project_path=str(checkout),
            workspace_config_root=str(tmp_path / "config"),
            path_overrides={},
        )
    )
    assert result.status == "success", result.stderr
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    timer = (unit_dir / f"{unit['label']}.timer").read_text(encoding="utf-8")
    service = (unit_dir / f"{unit['label']}.service").read_text(encoding="utf-8")
    assert f"OnUnitActiveSec={DAY_SECONDS}" in timer
    assert f'"-m" "{unit["module"]}"' in service
    assert f'"ONEX_LEDGER_CENSUS_ROOT={tmp_path}/omni"' in service
    # OMN-26318: the daily record's publish resolves its broker from the workspace config root; a unit without it exits 3.
    assert f'"ONEX_WORKSPACE_CONFIG_ROOT={tmp_path}/config"' in service


def test_the_census_tick_probe_answers_and_one_tick_keeps_the_daily_record(
    tmp_path: Path,
) -> None:
    unit = load_units()[CENSUS_UNIT]
    module = importlib.import_module(unit["module"])
    probe = io.StringIO()
    assert module.main(unit["probe_argv"], environ={}, stdout=probe) == 0
    assert json.loads(probe.getvalue()) == {"status": "probe_ok"}
    registry = tmp_path / "registry"
    registry.mkdir()
    registered = [
        str(row["name"])
        for row in yaml.safe_load(
            (NODES / CENSUS / "overlay.example.yaml").read_text(encoding="utf-8")
        )["repositories"]
    ]
    assert "omnimarket" in registered
    for name in registered:
        if name == "omnimarket":
            (registry / name).symlink_to(REPO_ROOT)
            continue
        # a registered repository the tick counts: an empty clone holds no reader, so it stays under its row
        (registry / name).mkdir()
        subprocess.run(
            ["git", "-C", str(registry / name), "init", "-q"],
            check=True,
            capture_output=True,
            env=scrub_git_location_env(os.environ),
        )
    out = io.StringIO()
    sent: list[dict[str, object]] = []

    class _Bus:
        def publish(self, **event: object) -> bool:
            sent.append(dict(event))
            return True

    code = module.main(
        ["--omni-home", str(registry), "--state-dir", str(tmp_path / "state")],
        environ={},
        stdout=out,
        publisher=_Bus(),
    )
    assert code == 0, out.getvalue()
    assert [e["topic"] for e in sent] == [
        "onex.evt.omnimarket.ledger-file-census-daily-record.v1"
    ]
    evidence = json.loads((tmp_path / "state" / "last-run.json").read_text("utf-8"))
    assert evidence["exit_code"] == 0
    assert evidence["published"] is True
    assert [r["repository"] for r in evidence["record"]["repositories"]] == registered
    assert "CENSUS-RECORD " in out.getvalue()


def test_the_dispatcher_declares_the_host_unit_the_contract_prose_promised() -> None:
    block = _dispatch_block(DISPATCH)
    assert isinstance(block["external_trigger"], str)
    assert block["external_trigger"].strip()
    trigger = _triggers(DISPATCH).host_unit
    assert trigger is not None
    assert trigger.unit == "lane-dispatch-systemd"
    units = load_units()
    assert trigger.runs_node(units, DISPATCH)
    assert units[trigger.unit]["schedule"]["start_interval"] == 60


def test_the_identity_provisioner_declares_an_on_demand_command_and_owner() -> None:
    on_demand = _triggers(IDENTITY).on_demand
    assert on_demand is not None
    assert on_demand.owner.strip()
    module = f"omnimarket.nodes.{IDENTITY}"
    assert f"python -m {module} run" in on_demand.command
    assert (NODES / IDENTITY / "__main__.py").is_file()
    assert importlib.import_module(f"{module}.runtime_dev_identity_provision")


def test_control_an_on_demand_declaration_without_an_owner_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelOnDemandTrigger.model_validate({"command": "uv run python -m x"})
    with pytest.raises(ValidationError):
        ModelOnDemandTrigger.model_validate({"command": " ", "owner": "someone"})


def test_control_a_host_unit_of_another_node_or_without_a_schedule_runs_nothing() -> (
    None
):
    trigger = _triggers(CENSUS).host_unit
    assert trigger is not None
    units = load_units()
    assert not trigger.runs_node(units, DISPATCH)
    assert not trigger.runs_node({}, CENSUS)
    no_schedule = {
        CENSUS_UNIT: {k: v for k, v in units[CENSUS_UNIT].items() if k != "schedule"}
    }
    assert not trigger.runs_node(no_schedule, CENSUS)
