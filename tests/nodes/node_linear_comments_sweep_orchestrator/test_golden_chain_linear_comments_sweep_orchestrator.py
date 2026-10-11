# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves, its handler runs a typed request to a typed terminal.

The decisions are the decision compute node's own handler over the captured
fixture of the retired workflow's sweep; the model calls are recorded answers.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from importlib.resources import files
from typing import cast

import yaml
from pydantic import JsonValue

from omnimarket.nodes.node_linear_comments_sweep_orchestrator.handlers.handler_linear_comments_sweep import (
    HandlerLinearCommentsSweep,
)
from omnimarket.nodes.node_linear_comments_sweep_orchestrator.models import (
    ModelLinearCommentsSweepRequest,
    ModelLinearCommentsSweepResult,
)

from .helpers import (
    CONTROL,
    ITEMS,
    NODE,
    SELECTED,
    SINCE,
    FixtureGateway,
    RealDecisions,
    public_overlay,
    request,
)

PACKAGE = f"omnimarket.nodes.{NODE}"


def _contract() -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        yaml.safe_load(files(PACKAGE).joinpath("contract.yaml").read_text()),
    )


def test_golden_chain_linear_comments_sweep_orchestrator() -> None:
    contract = _contract()
    assert contract["name"] == NODE
    assert contract["node_type"] == "orchestrator"
    routing = cast(dict[str, JsonValue], contract["handler_routing"])
    assert routing["routing_strategy"] == "operation_match"
    entries = cast(list[dict[str, JsonValue]], routing["handlers"])
    assert [entry["operation"] for entry in entries] == ["linear_comments_sweep"]
    entry = entries[0]
    handler_ref = cast(dict[str, str], entry["handler"])
    handler_type = getattr(
        importlib.import_module(handler_ref["module"]), handler_ref["name"]
    )
    assert handler_type is HandlerLinearCommentsSweep
    input_module, _, input_name = str(entry["input_model"]).rpartition(".")
    output_module, _, output_name = str(entry["output_model"]).rpartition(".")
    assert getattr(importlib.import_module(input_module), input_name) is (
        ModelLinearCommentsSweepRequest
    )
    assert getattr(importlib.import_module(output_module), output_name) is (
        ModelLinearCommentsSweepResult
    )
    # Definition-B: one typed request in, one typed result out.
    handle = HandlerLinearCommentsSweep.handle
    assert inspect.iscoroutinefunction(handle)
    assert list(inspect.signature(handle).parameters) == [
        "self",
        "request",
    ]


def test_contract_declares_every_topic_and_the_terminal() -> None:
    contract = _contract()
    bus = cast(dict[str, JsonValue], contract["event_bus"])
    subscribed = cast(list[str], bus["subscribe_topics"])
    published = cast(list[str], bus["publish_topics"])
    terminals = cast(dict[str, str], contract["terminal_events"])
    assert subscribed == ["onex.cmd.omnimarket.linear-comments-sweep-start.v1"]
    assert terminals == {
        "success": "onex.evt.omnimarket.linear-comments-sweep-completed.v1",
        "failure": "onex.evt.omnimarket.linear-comments-sweep-failed.v1",
    }
    assert contract["terminal_event"] == terminals["success"]
    runtime_dispatch = cast(dict[str, str], contract["runtime_dispatch"])
    assert runtime_dispatch["command_topic"] == subscribed[0]
    assert contract["externally_consumed_topics"] == [
        terminals["success"],
        terminals["failure"],
    ]
    assert terminals["success"] in published
    assert terminals["failure"] in published
    instances = {
        str(i["name"]): i
        for i in cast(
            list[dict[str, JsonValue]],
            cast(dict[str, JsonValue], bus["request_response"])["instances"],
        )
    }
    assert set(instances) == {"sweep_phase", "sweep_decision"}
    for instance in instances.values():
        assert instance["request_topic"] in published
        assert set(cast(dict[str, str], instance["reply_topics"])) == {
            "completed",
            "failed",
        }
    decision = importlib.import_module(
        "omnimarket.nodes.node_linear_comments_decision_compute"
    )
    decision_contract = yaml.safe_load(
        files(decision.__name__).joinpath("contract.yaml").read_text()
    )
    assert (
        instances["sweep_decision"]["request_topic"]
        == (decision_contract["runtime_dispatch"]["command_topic"])
    )
    assert (
        cast(dict[str, str], instances["sweep_decision"]["reply_topics"])["completed"]
        == (decision_contract["terminal_event"])
    )


def test_one_sweep_runs_the_captured_window_to_a_typed_terminal() -> None:
    gateway = FixtureGateway()
    decisions = RealDecisions()
    result = asyncio.run(
        HandlerLinearCommentsSweep(None, gateway, decisions, public_overlay()).handle(
            request()
        )
    )
    assert isinstance(result, ModelLinearCommentsSweepResult)
    needing = len(SELECTED)
    assert result.collected == len(ITEMS)
    assert result.needing == needing
    assert result.accepted == needing
    assert result.held == 0
    assert result.refused == len(ITEMS) - needing
    assert sum(result.refused_by_class.values()) == result.refused
    assert result.held_by_class == {
        "floor_refused": 0,
        "truncated": 0,
        "unsourced": 0,
        "transport": 0,
        "timeout": 0,
    }
    assert result.degraded is False
    assert result.control == CONTROL
    assert result.since == SINCE
    assert result.linear_file == "drafts/linear-comment-replies-2026-09-14T104031Z.md"
    assert result.metrics_file == "metrics/linear-comments.jsonl"
    assert result.written.rendered_by == "delegated"
    labels = gateway.labels()
    assert labels[0] == "collect:linear"
    assert labels[-1] == "write:linear-file"
    assert sorted(label.split(":")[0] for label in labels[1:-1]) == (
        ["delegate"] * needing + ["verify"] * needing
    )
    # Collection and writing and verification are agent phases; drafting is the document task class.
    by_prefix = {c.label.split(":")[0]: c for c in gateway.calls}
    assert by_prefix["collect"].task_type == "agent_delegation"
    assert (by_prefix["collect"].model, by_prefix["collect"].effort) == (
        "sonnet",
        "medium",
    )
    assert by_prefix["delegate"].task_type == "document"
    assert (by_prefix["verify"].model, by_prefix["verify"].effort) == ("opus", "high")
    assert (by_prefix["write"].model, by_prefix["write"].effort) == ("sonnet", "medium")
    assert decisions.kinds[0] == "decide_items"
    assert decisions.kinds[-1] == "tally"
    assert decisions.kinds.count("metrics_row") == needing
    # The cutoff reaches the collector exactly, and the write brief carries the report path.
    assert SINCE in gateway.calls[0].prompt
    assert result.linear_file in gateway.calls[-1].prompt
