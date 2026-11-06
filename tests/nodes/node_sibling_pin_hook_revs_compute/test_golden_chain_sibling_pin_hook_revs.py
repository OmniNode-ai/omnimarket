# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and the train night plans both homes (OMN-22641)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_sibling_pin_hook_revs_compute.models import (
    EnumHookRevsReason,
    ModelHookRevsCandidate,
    ModelSiblingPinHookRevsRequest,
)

NAME = "node_sibling_pin_hook_revs_compute"
PIN = "dev-20261105190003-87a6f5ddcbcc"
HOOK = "dev-20261105194015-1adbf46062bd"
MID = "dev-20261106041752-8377609527be"
NEWEST = "dev-20261106051752-0123456789ab"
OLD_HOOK = "dev-20261018024445-b6ee996befa1"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def _handler() -> Any:
    handler = _contract()["handler_routing"]["handlers"][0]["handler"]
    return getattr(importlib.import_module(handler["module"]), handler["name"])()


def _candidates(
    *tags: str, red: tuple[str, ...] = ()
) -> tuple[ModelHookRevsCandidate, ...]:
    return tuple(ModelHookRevsCandidate(tag=t, green=t not in red) for t in tags)


def test_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["side_effects"] == []
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.sibling-pin-hook-revs-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.sibling-pin-hook-revs-planned.v1"
    ]
    assert contract["terminal_event"] in bus["publish_topics"]
    assert {e["operation"] for e in contract["handler_routing"]["handlers"]} == {
        "plan_sibling_pin_hook_revs"
    }


def test_golden_chain_the_hooks_take_the_newest_tag_and_the_pin_the_one_under_it() -> (
    None
):
    """The 2026-11-06 train night: pin at the old core tag, two bound hook blocks, one old block."""
    plan = _handler().handle(
        ModelSiblingPinHookRevsRequest(
            pin_tag=PIN,
            hook_revs=(HOOK, OLD_HOOK, HOOK),
            candidates=_candidates(NEWEST, MID, HOOK, PIN),
        )
    )
    assert plan.hook_tag == NEWEST
    assert plan.hook_indexes == (0, 2)
    assert plan.pin_tag == MID
    assert plan.reason is EnumHookRevsReason.HOOKS_AND_PIN_MOVED


def test_golden_chain_no_bound_hook_lets_the_pin_take_the_newest_tag() -> None:
    plan = _handler().handle(
        ModelSiblingPinHookRevsRequest(
            pin_tag=PIN,
            hook_revs=(OLD_HOOK,),
            candidates=_candidates(MID, HOOK, PIN),
        )
    )
    assert (plan.hook_tag, plan.hook_indexes, plan.pin_tag) == (None, (), MID)
    assert plan.reason is EnumHookRevsReason.PIN_MOVED_NO_BOUND_HOOKS
