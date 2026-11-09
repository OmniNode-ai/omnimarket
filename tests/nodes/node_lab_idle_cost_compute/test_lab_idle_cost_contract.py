# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-idle cost node is reachable on its own contract and sums from the last start (OMN-22885).

`onex node` builds its initial payload from the contract's input_model, so the sum has to be the
input model of a contract of its own. These tests read that contract, build the request from the
contract's input model, and replay the 30 lab-idle FRICTION rows of 2026-11-07.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_lab_idle_cost_compute"
IDLE_ROWS = tuple(
    (Path(__file__).resolve().parent / "fixtures/lab_idle_friction_rows_2026-11-07.txt")
    .read_text()
    .splitlines()
)


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load((NODE / "contract.yaml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _class(dotted: str) -> Any:
    module, _, name = dotted.rpartition(".")
    return getattr(importlib.import_module(module), name)


def test_the_contract_names_a_pure_node_with_declared_topics() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    topics = contract["event_bus"]
    assert topics["subscribe_topics"] == [contract["runtime_dispatch"]["command_topic"]]
    assert topics["publish_topics"] == [contract["terminal_event"]]
    entry = contract["handler_routing"]["handlers"][0]
    assert entry["event_model"]["name"] == contract["input_model"].rpartition(".")[2]
    assert callable(
        getattr(
            importlib.import_module(entry["handler"]["module"]),
            entry["handler"]["name"],
        )().handle
    )


def test_the_node_is_registered_as_an_entry_point() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text("utf-8")
    assert (
        'node_lab_idle_cost_compute = "omnimarket.nodes.node_lab_idle_cost_compute"'
        in pyproject
    )


def test_the_30_rows_sum_to_7100_through_the_contract_input_model_with_no_since() -> (
    None
):
    contract = _contract()
    request_model = _class(contract["input_model"])
    result_model = _class(contract["output_model"])
    handler = _class(contract["handler"]["module"] + "." + contract["handler"]["class"])
    assert len(IDLE_ROWS) == 30
    result = handler().handle(request_model(rows=IDLE_ROWS))
    assert isinstance(result, result_model)
    assert (result.lane_minutes, result.rows, result.unreadable) == (7100.0, 30, 0)
    assert (
        result.segment == "lab-idle 7,100 lane-min (30 rows since 2026-11-07T00:35:28Z)"
    )


def test_an_empty_since_counts_from_the_last_orchestrator_claim() -> None:
    claim = "2026-11-07T10:00:00Z | CLAIM | lane=orchestrator-aaaa | ticket=OMN-1 | x"
    request_model = _class(_contract()["input_model"])
    handler = _class("omnimarket.nodes.node_lab_idle_cost_compute.HandlerLabIdleCost")
    result = handler().handle(request_model(rows=(claim, *IDLE_ROWS)))
    assert (result.lane_minutes, result.rows) == (500.0, 2)
    assert result.since == "2026-11-07T10:00:00Z"
    assert result.segment.startswith(
        "lab-idle 500 lane-min (2 rows since 2026-11-07T10:00:00Z"
    )


def test_an_empty_since_with_no_ledger_row_is_refused() -> None:
    request_model = _class(_contract()["input_model"])
    handler = _class("omnimarket.nodes.node_lab_idle_cost_compute.HandlerLabIdleCost")
    with pytest.raises(ValueError, match="since is empty"):
        handler().handle(request_model(rows=("# no rows",)))
