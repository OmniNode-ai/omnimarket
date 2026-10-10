# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged orchestrator contract resolves and a sweep runs through it (OMN-20676)."""

from __future__ import annotations

import importlib
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_metadata import MetadataSchema
from omnimarket.nodes.node_merge_sweep_orchestrator.handlers import (
    HandlerMergeSweepRun,
    LocalMergeSweepStages,
)
from omnimarket.nodes.node_merge_sweep_orchestrator.handlers.handler_merge_sweep_stages_local import (
    STAGE_OPERATIONS,
)

from ..node_merge_sweep_reading_compute.sweep_scenarios import load_recorded
from .support import RecordingStages, ScriptedRunner, run_request

NAME = "node_merge_sweep_orchestrator"
ROOT = Path(__file__).parents[3]
RECORDED = load_recorded()


def _contract(node: str = NAME) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        files(f"omnimarket.nodes.{node}").joinpath("contract.yaml").read_text()
    )
    return loaded


def _operations(node: str) -> set[str]:
    return {e["operation"] for e in _contract(node)["handler_routing"]["handlers"]}


def test_contract_declares_its_topics_and_no_shared_runtime_bus_block() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "orchestrator"
    assert "event_bus" not in contract
    dispatch = contract["runtime_dispatch"]
    assert (
        dispatch["command_topic"] == "onex.cmd.omnimarket.merge-sweep-run-requested.v1"
    )
    assert set(dispatch["terminal_events"].values()) == {
        "onex.evt.omnimarket.merge-sweep-run-completed.v1",
        "onex.evt.omnimarket.merge-sweep-run-failed.v1",
    }


def test_the_handler_the_contract_names_has_the_canonical_signature() -> None:
    (entry,) = _contract()["handler_routing"]["handlers"]
    handler = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    module, _, model = entry["input_model"].rpartition(".")
    request_type = getattr(importlib.import_module(module), model)
    hints = handler.handle.__annotations__
    assert hints["request"] in (request_type, request_type.__name__)
    assert "return" in hints


def test_the_wiring_plan_names_only_operations_the_sibling_nodes_declare() -> None:
    graph = _contract()["workflow_coordination"]["workflow_definition"][
        "execution_graph"
    ]
    steps = {n["node_id"]: n for n in graph["nodes"]}
    assert list(steps) == [
        "load_facts",
        "read_fleet",
        "plan_lanes",
        "render_brief",
        "run_lane",
        "decide_retry",
    ]
    planned: set[tuple[str, str]] = set()
    for step in steps.values():
        node, operation = step["step_config"]["node"], step["step_config"]["operation"]
        assert operation in _operations(node), (node, operation)
        for dependency in step.get("depends_on", []):
            assert dependency in steps
        planned.add((node, operation))
    assert planned == set(STAGE_OPERATIONS.values())


def test_every_stage_resolves_to_the_handler_its_node_contract_routes() -> None:
    stages = LocalMergeSweepStages()
    for stage, (node, operation) in STAGE_OPERATIONS.items():
        routed = next(
            e
            for e in _contract(node)["handler_routing"]["handlers"]
            if e["operation"] == operation
        )
        assert type(stages._stage(stage)).__name__ == routed["handler"]["name"]


def test_no_handler_imports_a_sibling_node_package() -> None:
    sources = (ROOT / "src" / "omnimarket" / "nodes" / NAME).rglob("*.py")
    for source in sources:
        for line in source.read_text().splitlines():
            assert (
                not line.startswith(
                    ("from omnimarket.nodes.", "import omnimarket.nodes.")
                )
                or NAME in line
            ), (source, line)


def test_entry_point_is_package_form_and_metadata_validates() -> None:
    with (ROOT / "pyproject.toml").open("rb") as f:
        entry_points = tomllib.load(f)["project"]["entry-points"]["onex.nodes"]
    assert entry_points.get(NAME) == f"omnimarket.nodes.{NAME}"
    meta = ROOT / "src" / "omnimarket" / "nodes" / NAME / "metadata.yaml"
    assert MetadataSchema(**yaml.safe_load(meta.read_text())).name == NAME


def test_a_sweep_runs_through_the_default_handler_and_dry_run_starts_nothing(
    tmp_path: Path,
) -> None:
    """The handler built as the runtime builds it (no arguments) reads and plans from the files."""
    spec = RECORDED["scenarios"][0]
    request = run_request(spec, RECORDED["base"], tmp_path, dispatch=False)
    result = HandlerMergeSweepRun().handle(request)
    assert result.ok, result.why
    assert result.plan is not None
    assert result.plan.lanes
    assert result.outcomes == []
    assert result.complete is (not result.unread)
    fields = {u.field for u in result.unread}
    assert not any(f.startswith(("files:", "ready_at:")) for f in fields)


def test_without_a_supplement_the_unread_facts_are_named_and_the_sweep_is_incomplete(
    tmp_path: Path,
) -> None:
    spec = RECORDED["scenarios"][0]
    request = run_request(
        spec, RECORDED["base"], tmp_path, supplement=None, dispatch=False
    )
    result = HandlerMergeSweepRun().handle(request)
    assert result.ok, result.why
    assert result.complete is False
    fields = {u.field for u in result.unread}
    assert "merge-files" in fields
    assert any(f.startswith("files:") for f in fields)


def test_the_runner_is_never_started_when_dispatch_is_off(tmp_path: Path) -> None:
    spec = RECORDED["scenarios"][0]
    runner = ScriptedRunner({})
    stages = RecordingStages(runner)
    HandlerMergeSweepRun(stages=stages).handle(
        run_request(spec, RECORDED["base"], tmp_path, dispatch=False)
    )
    assert runner.argvs == []


@pytest.mark.parametrize("claim_check", [None, "python3 /x/recheck.py <repo>#<n>"])
def test_the_callers_claim_check_command_is_in_every_brief(
    tmp_path: Path, claim_check: str | None
) -> None:
    spec = RECORDED["scenarios"][0]
    runner = ScriptedRunner(
        {
            f"sweep-0-{k}-{i}": [0] * 4
            for k in ("diagnose", "escalation", "fix", "land-chain-head")
            for i in range(1, 13)
        }
    )
    stages = RecordingStages(runner)
    result = HandlerMergeSweepRun(stages=stages, sleep=lambda _s: None).handle(
        run_request(spec, RECORDED["base"], tmp_path, claim_check_command=claim_check)
    )
    assert result.outcomes
    texts = [t for ts in stages.briefs.values() for t in ts]
    expected = claim_check or "sweep_read.py"
    assert all(expected in t for t in texts)
