# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A node that nothing runs is refused: its contract names a tick, a schedule, a host unit, a caller or an on-demand command.

The failing fixture is the census contract as it stood before it declared a trigger: ``external_trigger`` true, no
topic, no schedule. The control fixtures are the same node with each accepted declaration, and each declaration is
refused again when what it names does not hold (a unit of another node, a caller that is a test or does not name the
node, an on-demand command with no owner).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_trigger_declaration_check_effect.handlers import (
    HandlerTriggerDeclarationCheck,
)
from omnimarket.nodes.node_trigger_declaration_check_effect.models import (
    EnumTriggerFindingReason,
    ModelTriggerCheckRequest,
    ModelTriggerCheckResult,
)
from omnimarket.nodes.node_trigger_declaration_check_effect.runtime_trigger_declaration_check import (
    changed_node_contracts,
    main,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "node_trigger_check"
CENSUS = "node_work_ledger_file_census_effect"
INSTALLER = "node_host_installer_registry_units"
CHECK_NODE = "node_trigger_declaration_check_effect"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"


def _contract_path(node: str) -> str:
    return f"src/omnimarket/nodes/{node}/contract.yaml"


def _write_node(root: Path, node: str, contract: dict[str, Any]) -> str:
    path = root / _contract_path(node)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return _contract_path(node)


def _write_installer(root: Path, units: dict[str, Any]) -> None:
    _write_node(root, INSTALLER, {"units": units})


def _census_before() -> dict[str, Any]:
    loaded = yaml.safe_load(
        (FIXTURES / "census_contract_before_triggers.yaml").read_text("utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def _check(root: Path, *contracts: str) -> ModelTriggerCheckResult:
    return HandlerTriggerDeclarationCheck().handle(
        ModelTriggerCheckRequest(root=str(root), contracts=contracts)
    )


def _census_unit(node: str = CENSUS) -> dict[str, Any]:
    return {
        "census-unit": {
            "schedule": {"start_interval": 86400},
            "module": f"omnimarket.nodes.{node}.runtime_tick",
        }
    }


def test_the_census_contract_before_it_declared_a_trigger_is_refused(
    tmp_path: Path,
) -> None:
    """AC1 falsifier: the fixture copy of the census contract of omnimarket 6607 fails the check."""
    contract = _write_node(tmp_path, CENSUS, _census_before())
    result = _check(tmp_path, contract)
    assert not result.passed
    assert result.needing_trigger == 1
    (finding,) = result.findings
    assert finding.node == CENSUS
    assert finding.reason is EnumTriggerFindingReason.NO_TRIGGER_DECLARED


def test_control_a_node_with_a_schedule_passes(tmp_path: Path) -> None:
    contract = _census_before()
    contract["schedule"] = {"unit_name": "census-daily", "timeout_seconds": 300}
    path = _write_node(tmp_path, CENSUS, contract)
    result = _check(tmp_path, path)
    assert result.passed
    assert result.needing_trigger == 1


def test_control_a_runtime_tick_subscription_with_a_schedule_passes(
    tmp_path: Path,
) -> None:
    contract = _census_before()
    contract["event_bus"] = {"subscribe_topics": [TICK_TOPIC], "publish_topics": []}
    contract["config"] = {CENSUS: {"schedule": {"interval_seconds": 3600}}}
    assert _check(tmp_path, _write_node(tmp_path, CENSUS, contract)).passed


def test_a_runtime_tick_subscription_with_no_schedule_is_refused(
    tmp_path: Path,
) -> None:
    contract = _census_before()
    contract["event_bus"] = {"subscribe_topics": [TICK_TOPIC], "publish_topics": []}
    result = _check(tmp_path, _write_node(tmp_path, CENSUS, contract))
    assert [f.reason for f in result.findings] == [
        EnumTriggerFindingReason.NO_TRIGGER_DECLARED
    ]


def test_control_a_node_that_subscribes_to_a_topic_needs_no_trigger(
    tmp_path: Path,
) -> None:
    contract = _census_before()
    contract["runtime_dispatch"] = {}
    contract["event_bus"] = {"subscribe_topics": ["onex.cmd.x.y.v1"]}
    result = _check(tmp_path, _write_node(tmp_path, CENSUS, contract))
    assert result.passed
    assert result.needing_trigger == 0


def test_a_node_with_no_event_bus_block_needs_a_trigger(tmp_path: Path) -> None:
    result = _check(
        tmp_path, _write_node(tmp_path, CENSUS, {"name": CENSUS, "node_type": "x"})
    )
    assert result.needing_trigger == 1
    assert not result.passed


def test_control_a_host_unit_that_runs_the_node_passes(tmp_path: Path) -> None:
    _write_installer(tmp_path, _census_unit())
    contract = _census_before()
    contract["runtime_dispatch"] = {
        "external_trigger": True,
        "host_unit": {"unit": "census-unit"},
    }
    assert _check(tmp_path, _write_node(tmp_path, CENSUS, contract)).passed


@pytest.mark.parametrize(
    "units",
    [
        {},
        _census_unit("node_something_else"),
        {"census-unit": {"module": f"omnimarket.nodes.{CENSUS}.runtime_tick"}},
    ],
    ids=["unit-absent", "unit-of-another-node", "unit-without-schedule"],
)
def test_a_host_unit_that_does_not_run_the_node_is_refused(
    tmp_path: Path, units: dict[str, Any]
) -> None:
    _write_installer(tmp_path, units)
    contract = _census_before()
    contract["runtime_dispatch"] = {
        "external_trigger": True,
        "host_unit": {"unit": "census-unit"},
    }
    result = _check(tmp_path, _write_node(tmp_path, CENSUS, contract))
    assert [f.reason for f in result.findings] == [
        EnumTriggerFindingReason.INVALID_DECLARATION
    ]


def test_control_an_on_demand_command_with_an_owner_passes(tmp_path: Path) -> None:
    contract = _census_before()
    contract["runtime_dispatch"] = {
        "external_trigger": True,
        "on_demand": {"command": f"uv run python -m {CENSUS}", "owner": "a team"},
    }
    assert _check(tmp_path, _write_node(tmp_path, CENSUS, contract)).passed


def test_an_on_demand_command_with_no_owner_is_refused(tmp_path: Path) -> None:
    contract = _census_before()
    contract["runtime_dispatch"] = {
        "external_trigger": True,
        "on_demand": {"command": f"uv run python -m {CENSUS}"},
    }
    result = _check(tmp_path, _write_node(tmp_path, CENSUS, contract))
    assert [f.reason for f in result.findings] == [
        EnumTriggerFindingReason.INVALID_DECLARATION
    ]


def _caller_contract(path: str) -> dict[str, Any]:
    contract = _census_before()
    contract["runtime_dispatch"] = {
        "external_trigger": True,
        "callers": [{"path": path}],
    }
    return contract


def test_control_a_caller_file_that_names_the_node_passes(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "check.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(f"run: python -m omnimarket.nodes.{CENSUS}\n", "utf-8")
    contract = _write_node(
        tmp_path, CENSUS, _caller_contract(".github/workflows/check.yml")
    )
    assert _check(tmp_path, contract).passed


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (".github/workflows/absent.yml", None),
        (".github/workflows/other.yml", "run: echo nothing\n"),
        (f"src/omnimarket/nodes/{CENSUS}/contract.yaml", f"{CENSUS}\n"),
        (f"src/omnimarket/nodes/{CENSUS}/runtime_x.py", f"{CENSUS}\n"),
        ("tests/test_calls.py", f"{CENSUS}\n"),
        ("/etc/hostname", None),
        ("../outside.yml", None),
    ],
    ids=[
        "path-absent",
        "path-does-not-name-the-node",
        "own-contract",
        "own-module",
        "a-test",
        "absolute-path",
        "outside-the-repository",
    ],
)
def test_a_caller_that_does_not_run_the_node_is_refused(
    tmp_path: Path, path: str, body: str | None
) -> None:
    if body is not None:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, "utf-8")
    contract = _write_node(tmp_path, CENSUS, _caller_contract(path))
    result = _check(tmp_path, contract)
    assert [f.reason for f in result.findings] == [
        EnumTriggerFindingReason.INVALID_DECLARATION
    ]


def test_an_unreadable_contract_is_a_finding_not_a_pass(tmp_path: Path) -> None:
    path = tmp_path / _contract_path(CENSUS)
    path.parent.mkdir(parents=True)
    path.write_text("- not\n- a mapping\n", "utf-8")
    result = _check(tmp_path, _contract_path(CENSUS))
    assert [f.reason for f in result.findings] == [
        EnumTriggerFindingReason.UNREADABLE_CONTRACT
    ]


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e.com",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def test_the_changed_contracts_are_the_nodes_a_diff_touches(tmp_path: Path) -> None:
    _write_node(tmp_path, "node_a_effect", {"name": "a"})
    _write_node(tmp_path, "node_b_effect", {"name": "b"})
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    handler = tmp_path / "src/omnimarket/nodes/node_a_effect/handler.py"
    handler.write_text("x = 1\n", "utf-8")
    (tmp_path / "docs.md").write_text("not a node\n", "utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "change a node's handler")
    assert changed_node_contracts(tmp_path, base) == (_contract_path("node_a_effect"),)


def test_the_cli_exits_one_on_a_refused_node_and_zero_on_a_declared_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    refused = _write_node(tmp_path, CENSUS, _census_before())
    assert main(["--root", str(tmp_path), "--contract", refused]) == 1
    assert CENSUS in capsys.readouterr().out
    declared = _census_before()
    declared["schedule"] = {"unit_name": "census-daily"}
    ok = _write_node(tmp_path, CENSUS, declared)
    assert main(["--root", str(tmp_path), "--contract", ok]) == 0


def test_the_check_node_declares_the_job_that_runs_it() -> None:
    """The check passes on its own diff: its contract names the workflow that calls it."""
    result = _check(REPO_ROOT, _contract_path(CHECK_NODE))
    assert result.needing_trigger == 1
    assert result.passed, result.findings


@pytest.mark.parametrize("node", [CENSUS, "node_lane_dispatch_intake_effect"])
def test_the_nodes_that_declare_a_host_unit_pass_on_dev(node: str) -> None:
    result = _check(REPO_ROOT, _contract_path(node))
    assert result.passed, result.findings


def test_the_dev_identity_provisioner_passes_on_its_on_demand_command() -> None:
    node = "node_dev_identity_provision_effect"
    assert _check(REPO_ROOT, _contract_path(node)).passed


@pytest.mark.all_nodes_run
def test_every_node_of_the_repository_declares_a_trigger() -> None:
    """``--all-nodes``: the AC3 falsifier. Red on dev until each node of the list declares a trigger."""
    result = HandlerTriggerDeclarationCheck().handle(
        ModelTriggerCheckRequest(root=str(REPO_ROOT), all_nodes=True)
    )
    assert result.checked > 1000
    assert result.passed, f"{len(result.findings)} nodes declare no trigger"
