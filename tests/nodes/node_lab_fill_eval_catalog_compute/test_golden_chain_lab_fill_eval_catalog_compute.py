# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain for the eval catalog check: the packaged contract routes to a handler that judges the overlay."""

from __future__ import annotations

import importlib
from importlib.resources import files

import pytest
import yaml

pytestmark = pytest.mark.unit

NAME = "node_lab_fill_eval_catalog_compute"
COMMAND = "onex.cmd.omnimarket.lab-fill-eval-catalog-check-requested.v1"
TERMINAL = "onex.evt.omnimarket.lab-fill-eval-catalog-checked.v1"
EXPECTED_FEEDS = {
    "escalation-forced": "R3",
    "malformed-and-injected-tasks": "R9",
    "rung-killed-or-slowed": "R4",
    "routing-correctness": "R7",
    "capability-by-task-class": "experiment",
    "task-complexity-acceptance": "experiment",
    "variance-repeat": "experiment",
}


def _package_file(name: str) -> str:
    return files(f"omnimarket.nodes.{NAME}").joinpath(name).read_text()


def _chain():
    contract = yaml.safe_load(_package_file("contract.yaml"))
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL]
    assert contract["terminal_event"] == TERMINAL
    (entry,) = contract["handler_routing"]["handlers"]
    handler_type = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    in_mod, _, in_name = entry["input_model"].rpartition(".")
    out_mod, _, out_name = entry["output_model"].rpartition(".")
    return (
        handler_type,
        getattr(importlib.import_module(in_mod), in_name),
        getattr(importlib.import_module(out_mod), out_name),
    )


def test_golden_chain_the_packaged_overlay_passes_with_every_feeds_declared() -> None:
    handler_type, request_type, result_type = _chain()
    request = request_type.model_validate(
        yaml.safe_load(_package_file("overlay.example.yaml"))
    )
    result = handler_type().handle(request)
    assert isinstance(result, result_type)
    assert result.ok is True
    assert result.defects == ()
    assert result.checked == 7
    assert {e.id: e.feeds for e in request.evals} == EXPECTED_FEEDS


def test_error_chain_a_missing_unknown_or_duplicate_feeds_is_named_by_eval_id() -> None:
    handler_type, request_type, _ = _chain()
    doc = yaml.safe_load(_package_file("overlay.example.yaml"))
    del doc["evals"][0]["feeds"]
    doc["evals"][1]["feeds"] = "R10"
    doc["evals"][2]["id"] = doc["evals"][3]["id"]
    result = handler_type().handle(request_type.model_validate(doc))
    assert result.ok is False
    by_id = {(d.eval_id, d.reason.split(" ")[0]) for d in result.defects}
    assert (doc["evals"][0]["id"], "feeds") in by_id
    assert any(
        d.eval_id == doc["evals"][1]["id"] and d.feeds == "R10" for d in result.defects
    )
    assert any(d.reason == "duplicate eval id" for d in result.defects)
    assert "R10" in result.summary


def test_error_chain_an_empty_catalog_is_not_a_pass() -> None:
    handler_type, request_type, _ = _chain()
    result = handler_type().handle(request_type())
    assert result.ok is False
    assert result.checked == 0
    assert result.defects[0].reason == "the catalog lists no eval"
