# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Direct tick target and timing assertions extracted from the controller diff (OMN-17427)."""

from omnimarket.nodes.node_landing_tick_bounds_compute import HandlerLandingTickBounds
from omnimarket.nodes.node_landing_tick_bounds_compute.handlers import (
    handler_landing_tick_bounds as tick_bounds,
)
from omnimarket.nodes.node_landing_tick_bounds_compute.models import (
    ModelLandingTickTargetRequest,
)


def test_tick_target_ranks_phases_not_counts() -> None:
    """The node ranks only the leaf ``*_s`` phases: never ``total_s``, the ``read_s`` and ``perform_s`` sums of other
    phases, or a round count."""
    got = tick_bounds.judge(
        {
            "read_s": 50.0,
            "read_ledger_s": 3.0,
            "decide_s": 9.5,
            "enrich_rounds": 4,
            "lab_s": 40.0,
            "perform_s": 45.0,
            "merge_s": 1.0,
            "total_s": 60.0,
        },
        {"LANDING_TICK_TARGET_S": "30"},
    )
    assert got.receipt() == {
        "target_s": 30.0,
        "total_s": 60.0,
        "over": True,
        "top": [["lab_s", 40.0], ["decide_s", 9.5], ["read_ledger_s", 3.0]],
    }
    assert (
        got.alarm()
        == "TICK-SLOW total_s=60 target_s=30 top=lab_s:40,decide_s:10,read_ledger_s:3"
    )
    inside = tick_bounds.judge({"lab_s": 4.0, "total_s": 5.0}, {})
    assert inside.target_s == 120.0
    assert inside.over is False
    assert inside.alarm() == ""
    assert tick_bounds.budget_s({}) == 720.0
    assert tick_bounds.deadline_s({"LANDING_TICK_DEADLINE_S": "x"}) == 1500.0


def test_tick_over_zero_target_names_the_slowest_phases() -> None:
    # Supply the measured timing directly; collecting timing belongs to the controller.
    timing = {"lab_s": 4.0, "total_s": 5.0}
    result = HandlerLandingTickBounds().handle(
        ModelLandingTickTargetRequest(
            timing=timing, target_s=tick_bounds.target_s({"LANDING_TICK_TARGET_S": "0"})
        )
    )
    target = result.receipt()
    assert target["target_s"] == 0.0
    assert target["over"] is True
    assert target["total_s"] == timing["total_s"]
    assert 1 <= len(result.top) <= 3
    assert all(
        str(phase).endswith("_s") and phase != "total_s" for phase, _s in result.top
    )
    seconds = [s for _p, s in result.top]
    assert seconds == sorted(seconds, reverse=True)
    assert "TICK-SLOW total_s=" in result.alarm()
    assert "target_s=0" in result.alarm()


def test_tick_inside_default_target_raises_no_alarm() -> None:
    result = HandlerLandingTickBounds().handle(
        ModelLandingTickTargetRequest(
            timing={"lab_s": 4.0, "total_s": 5.0}, target_s=tick_bounds.target_s({})
        )
    )
    assert result.target_s == 120.0
    assert result.over is False
    assert result.alarm() == ""


def test_contract_default_handler_resolves_to_the_packaged_handler() -> None:
    import importlib
    from pathlib import Path

    import yaml

    import omnimarket.nodes.node_landing_tick_bounds_compute as package

    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    assert contract["name"] == "node_landing_tick_bounds_compute"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["descriptor"]["runtime_profiles"] == []
    assert contract["runtime_dispatch"]["external_trigger"].strip()
    assert "terminal_event" not in contract
    module_name, class_name = contract["handler_routing"]["default_handler"].split(":")
    handler_class = getattr(importlib.import_module(module_name), class_name)
    assert callable(getattr(handler_class(), "handle", None))
