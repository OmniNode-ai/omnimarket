# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic captured parity, registered contract, in-memory golden and error chains."""

from __future__ import annotations

import ast
import importlib
import json
import tempfile
from collections.abc import Iterator
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_friction_rollup_compute as node_package
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)
from omnimarket.nodes.node_friction_rollup_compute.models.model_friction_rollup import (
    ModelFrictionRollupRequest,
    ModelFrictionRollupResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.friction-rollup-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.friction-rollup-completed.v1"
PARITY = json.loads((ROOT / "tests/fixtures/friction_rollup_parity.json").read_text())
HANDLER = HandlerFrictionRollup()


def decide(payload: dict[str, Any]) -> ModelFrictionRollupResult:
    return HANDLER.handle(ModelFrictionRollupRequest.model_validate(payload))


def _as_old(result: ModelFrictionRollupResult) -> dict[str, Any]:
    return {
        "report": result.report,
        "markdown": result.markdown,
        "json_text": result.json_text,
    }


@pytest.mark.parametrize("case", PARITY["cases"], ids=lambda c: str(c["name"]))
def test_old_behavior_parity(case: dict[str, Any]) -> None:
    result = decide(case["request"])
    assert result.report == case["expected"]["report"]
    assert result.markdown == case["expected"]["markdown"]
    assert result.json_text == case["expected"]["json_text"]


def test_parity_fixture_fires_every_signal_and_cost_variant() -> None:
    cases = {c["name"]: c for c in PARITY["cases"]}
    assert len(cases) >= 14
    names = {
        "correction-row",
        "lane-refused-or-halted",
        "ci-rerun-repeated",
        "merge-queue-ejection",
        "drain-blocked-repeat",
        "delegation-failed-or-skipped",
    }
    fired = {
        name
        for c in cases.values()
        for name, signal in c["expected"]["report"]["signals"].items()
        if signal["count"] > 0
    }
    assert fired == names
    negative = cases["failed-zero-negative"]["expected"]["report"]["signals"]
    assert all(s["count"] == 0 for s in negative.values())
    costs = cases["cost-variants"]["expected"]["report"]
    assert costs["totals"]["cost_minutes"] == 531
    rows = costs["largest_costs"] + costs["unparseable_costs"]
    for text, minutes in [
        ("~5 lane-minutes", 5),
        ("2h", 120),
        ("1-2 hours", 90),
        ("about 30 min", 30),
        ("unknown effort", None),
        ("7 minutes", 7),
        ("~0.3 lane-hours plus roughly 3 orchestrator-minutes", 21),
        ("<4 hrs", 240),
        ("10\u201320 mins", 15),
        ("cost=0 m", 0),
        ("cost=1 min | costs: 2 min", 3),
    ]:
        # Isolate each variant through its original source text and compare the captured full report.
        source = cases["cost-variants"]["request"]["sources"][0]["text"]
        line = next(line for line in source.splitlines() if text in line)
        request = {
            **cases["cost-variants"]["request"],
            "sources": [{"display": "ledger.md", "kind": "markdown", "text": line}],
        }
        report = decide(request).report
        assert report["totals"]["cost_minutes"] == (minutes or 0)
        assert report["totals"]["unparseable_cost_rows"] == int(minutes is None)
    assert rows
    assert (
        cases["duplicate-live-archive"]["expected"]["report"]["duplicates_dropped"] == 1
    )
    assert (
        cases["archive-missing"]["expected"]["report"]["sources"][0]["status"]
        == "missing"
    )
    assert any(
        v["how"] == "fuzzy"
        for v in cases["fuzzy-tie"]["expected"]["report"]["class_mapping"].values()
    )
    assert any(
        v["how"] == "alias"
        for v in cases["normalisation-and-alias"]["expected"]["report"][
            "class_mapping"
        ].values()
    )
    assert cases["umbrella-only"]["expected"]["report"]["flags"][
        "recurring_without_ticket"
    ]
    assert not cases["recurring-with-owner"]["expected"]["report"]["flags"][
        "recurring_without_ticket"
    ]
    assert (
        cases["jsonl-valid-and-malformed"]["expected"]["report"]["jsonl"][
            "malformed_lines"
        ]
        == 5
    )
    largest = cases["largest-cost-order"]["expected"]["report"]["largest_costs"]
    assert [r["cost_minutes"] for r in largest] == [99, 99, 12, 8, 5]
    assert len(cases["trend-three-hour"]["expected"]["report"]["trend"]) == 2


def test_result_attributes_and_determinism() -> None:
    case = PARITY["cases"][5]
    result = decide(case["request"])
    assert result.report == case["expected"]["report"]
    assert result.markdown == case["expected"]["markdown"]
    assert result.json_text == case["expected"]["json_text"]
    assert decide(case["request"]) == result
    assert (
        ModelFrictionRollupResult.model_validate_json(result.model_dump_json())
        == result
    )


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["descriptor"]["idempotent"] is True
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["externally_consumed_topics"] == [TERMINAL_TOPIC]
    assert (
        contract["handler_routing"]["handlers"][0]["operation"]
        == "compute_friction_rollup"
    )
    assert set(contract["outputs"]) == {"report", "markdown", "json_text"}
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeFrictionRollupCompute, handler_type)
    for side, model in [
        ("input_model", ModelFrictionRollupRequest),
        ("output_model", ModelFrictionRollupResult),
    ]:
        assert (
            getattr(
                importlib.import_module(contract[side]["module"]),
                contract[side]["name"],
            )
            is model
        )
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_friction_rollup_compute"].load() is node_package


@pytest.mark.parametrize(
    "override",
    [
        {"since": "2026-10-09T06:00:00Z"},
        {"since": "2026-10-10"},
        {"until": "bad"},
        {"generated_at": "bad"},
        {"bucket_hours": 0},
        {"bucket_hours": -1},
        {"aliases": ["bad"]},
        {"aliases": ["=widget"]},
        {"aliases": ["widget="]},
        {"sources": [{"display": "ledger.md", "kind": "markdown", "text": None}]},
        {"sources": [{"display": "events.jsonl", "kind": "jsonl", "text": None}]},
        {"sources": [{"display": "archive", "kind": "missing", "text": ""}]},
        {"sources": [{"display": "ledger.md", "kind": "unknown", "text": ""}]},
        {"surprise": 1},
    ],
)
def test_error_chain_refuses_before_handler(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        decide({**PARITY["cases"][0]["request"], **override})


def test_request_is_frozen_and_has_no_umbrella_default() -> None:
    request = ModelFrictionRollupRequest.model_validate(PARITY["cases"][0]["request"])
    with pytest.raises(ValidationError, match="frozen"):
        request.bucket_hours = 3
    missing = request.model_dump()
    del missing["umbrella_tickets"]
    with pytest.raises(ValidationError, match="umbrella_tickets"):
        ModelFrictionRollupRequest.model_validate(missing)


def test_handler_imports_and_calls_stay_pure() -> None:
    allowed = {
        "__future__",
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "difflib",
        "json",
        "re",
        "typing",
        "pydantic",
        "omnimarket",
    }
    for path in NODE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name.split(".")[0] in allowed for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] in allowed
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in {"open", "print", "input"}
                elif isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in {
                        "now",
                        "time",
                        "read_text",
                        "write_text",
                        "read_bytes",
                        "write_bytes",
                    }


@pytest.fixture
def scratch_path() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir=ROOT / ".claude_scratch") as directory:
        yield Path(directory)


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_bus_matches_capture(scratch_path: Path) -> None:
    case = next(c for c in PARITY["cases"] if c["name"] == "cost-variants")
    runtime = await _run(scratch_path, case["request"])
    assert isinstance(runtime.handler_result, ModelFrictionRollupResult)
    assert _as_old(runtime.handler_result) == case["expected"]


@pytest.mark.asyncio
async def test_error_chain_over_bus_has_no_result(scratch_path: Path) -> None:
    runtime = await _run(scratch_path, {})
    assert runtime.handler_result is None
    bad = scratch_path / "bad.json"
    bad.write_text(json.dumps({**PARITY["cases"][0]["request"], "aliases": ["bad"]}))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=scratch_path / "state2",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
