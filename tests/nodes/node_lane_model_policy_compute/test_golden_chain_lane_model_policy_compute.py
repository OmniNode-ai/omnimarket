# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ceiling, aliases, exact pins, capacity and canonical contract proof."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from itertools import product

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lane_model_policy_compute.handlers import (
    handler_lane_model_policy as policy,
)
from omnimarket.nodes.node_lane_model_policy_compute.models.model_lane_model_policy import (
    ModelLaneModelPolicy,
)
from omnimarket.nodes.node_lane_model_policy_compute.models.model_lane_model_policy_request import (
    ModelLaneModelPolicyRequest,
)


def test_no_input_combination_can_yield_opus():
    handler = policy.HandlerLaneModelPolicy()
    for engine, tier, band, kind, exact in product(
        (
            "auto",
            "claude",
            "opus",
            "claude_opus",
            "claude-sonnet",
            "codex_high",
            "glm",
            "code-edit",
            "unknown",
        ),
        (None, "sonnet", "opus", "unknown"),
        (None, "B0", "B1", "B1c", "B2", "B3", "B4", "B5", "unknown"),
        ("build", "small-fix", "host-tick-repair"),
        (None, "sonnet", "opus", "claude_opus"),
    ):
        result = handler.handle(
            ModelLaneModelPolicyRequest(
                requested_engine=engine,
                requested_tier=tier,
                band=band,
                lane_kind=kind,
                required_model=exact,
                code_edit_ready=True,
                route_ceiling="opus",
            )
        )
        assert result.model != "opus", (engine, tier, band, kind, exact, result)
        assert result.engine != "claude_opus"
        assert result.ceiling != "opus"
        assert result.allowed == (result.model is not None)


@pytest.mark.parametrize(
    ("facts", "model"),
    [
        ({"lane_kind": "build"}, "codex"),
        ({"lane_kind": "small-fix"}, "glm"),
        ({"lane_kind": "small-fix", "code_edit_ready": True}, "code-edit"),
        ({"lane_kind": "small-fix", "available_engines": ("claude", "codex")}, "codex"),
        ({"lane_kind": "small-fix", "available_engines": ("claude",)}, "sonnet"),
        (
            {
                "lane_kind": "build",
                "requested_engine": "claude",
                "requested_tier": "opus",
            },
            "sonnet",
        ),
        ({"lane_kind": "build", "band": "B4", "requested_engine": "claude"}, "sonnet"),
        ({"lane_kind": "host-tick-repair"}, "sonnet"),
        ({"lane_kind": "build", "required_engine": "claude"}, "sonnet"),
        ({"lane_kind": "build", "required_engine": "opus"}, None),
        ({"lane_kind": "build", "required_model": "claude_opus"}, None),
        ({"lane_kind": "build", "route_ceiling": "glm"}, None),
        ({"lane_kind": "build", "available_models": ()}, None),
        ({"lane_kind": "build", "available_models": ("opus",)}, None),
        ({"lane_kind": "build", "band": "B0"}, None),
        ({"lane_kind": "build", "requested_tier": "unknown"}, None),
    ],
)
def test_kind_capacity_and_pins(facts, model):
    result = policy.resolve_lane_model(**facts)
    assert result.model == model
    assert result.policy_source.endswith("contract.yaml#lane_model_policy")


def test_lowering_the_declared_ceiling_changes_consumers(monkeypatch):
    lowered = ModelLaneModelPolicy.model_validate(
        {**policy.load_policy().model_dump(), "ceiling": "codex"}
    )
    monkeypatch.setattr(policy, "load_policy", lambda: lowered)
    assert policy.resolve_lane_model(lane_kind="build").model == "codex"
    assert not policy.resolve_lane_model(
        lane_kind="build", requested_engine="claude"
    ).allowed
    assert policy.lane_model_refusal("sonnet") is not None


def test_contract_binding_and_installed_entry_point():
    contract = yaml.safe_load(policy.CONTRACT_PATH.read_text())
    binding = contract["handler"]
    cls = getattr(importlib.import_module(binding["module"]), binding["class"])
    request_module, _, request_name = binding["input_model"].rpartition(".")
    request = getattr(importlib.import_module(request_module), request_name)(
        lane_kind="build"
    )
    result = cls().handle(request)
    result_module, _, result_name = binding["output_model"].rpartition(".")
    assert isinstance(
        result, getattr(importlib.import_module(result_module), result_name)
    )
    entry = next(
        e for e in entry_points(group="onex.nodes") if e.name == contract["name"]
    )
    node = entry.load().NodeLaneModelPolicyCompute
    assert node.contract_path == policy.CONTRACT_PATH
    assert node().handle(request) == result
    metadata = yaml.safe_load(
        (policy.CONTRACT_PATH.parent / "metadata.yaml").read_text()
    )
    assert metadata["name"] == contract["name"]


def test_malformed_policy_refuses_instead_of_falling_back():
    for update in (
        {"ceiling": "missing"},
        {"order": ["sonnet", "sonnet"]},
        {"aliases": {"bad": "missing"}},
    ):
        with pytest.raises(ValidationError):
            ModelLaneModelPolicy.model_validate(
                {**policy.load_policy().model_dump(), **update}
            )


def test_fill_reads_the_declared_ceiling_instead_of_a_dispatcher_constant(monkeypatch):
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        HandlerIdleSlotFill,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models import (
        ModelIdleSlotFillRequest,
    )

    request = ModelIdleSlotFillRequest(
        now="2026-10-26T19:59:00Z",
        hosts=[{"name": "host-a", "admission": {"claude_slots": 1}}],
        lines=["MISSING lab-headroom:host-a | free Claude slots 1"],
        backlog=[
            {
                "kind": "lab-repair",
                "key": "recorded",
                "ticket": "OMN-21396",
                "brief": "Replay the recorded repair.",
            }
        ],
    )
    assert HandlerIdleSlotFill().handle(request).sends[0]["model"] == "sonnet"
    lowered = ModelLaneModelPolicy.model_validate(
        {**policy.load_policy().model_dump(), "ceiling": "codex"}
    )
    monkeypatch.setattr(policy, "load_policy", lambda: lowered)
    result = HandlerIdleSlotFill().handle(request)
    assert result.sends == []
    assert result.idle_after == 1


@pytest.mark.asyncio
async def test_contract_terminal_event_carries_the_policy_decision():
    import json

    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_core.runtime.runtime_local_adapter import LocalRuntimeBusAdapter

    from omnimarket.nodes.node_lane_model_policy_compute.models.model_lane_model_policy_result import (
        ModelLaneModelPolicyResult,
    )

    contract = yaml.safe_load(policy.CONTRACT_PATH.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.lane-model-policy-resolved.v1"
    )
    bus = EventBusInmemory()
    adapter = LocalRuntimeBusAdapter(
        handler=policy.HandlerLaneModelPolicy(),
        handler_name="HandlerLaneModelPolicy",
        input_model_cls=ModelLaneModelPolicyRequest,
        output_topic=contract["terminal_event"],
        bus=bus,
    )
    await bus.start()
    try:
        await bus.subscribe(
            contract["runtime_dispatch"]["command_topic"],
            on_message=adapter.on_message,
            group_id="policy-request",
        )
        await bus.publish(
            contract["runtime_dispatch"]["command_topic"],
            None,
            ModelLaneModelPolicyRequest(
                requested_engine="claude",
                requested_tier="opus",
                band="B4",
                lane_kind="build",
            )
            .model_dump_json()
            .encode(),
        )
        history = await bus.get_event_history(topic=contract["terminal_event"])
        assert len(history) == 1
        result = ModelLaneModelPolicyResult.model_validate(json.loads(history[0].value))
        assert result.allowed
        assert result.model == "sonnet"
        assert result.ceiling == "sonnet"
    finally:
        await bus.close()
