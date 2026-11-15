# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-24296: per-task-kind rungs in the routing overlay stay honest.

Failing controls first: an unknown key in any section is refused, a ``local``
kind below the route's ``min_f1`` is refused naming the kind and its f1 while
a ``current`` kind is held to no floor, a route is refused when its task class
has no escalation chain or its local backend is undeclared, two routes may not
answer one question twice, and the guard's measurement may not claim more
correct answers than it measured. An overlay without the section, and the
shipped empty overlay, carry ``task_kind_rungs`` ``None`` and change nothing.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.model_delegation_routing_overlay import (
    EMPTY_DELEGATION_ROUTING_OVERLAY,
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.model_task_kind_rungs_overlay import (
    EnumTaskKindRung,
)

pytestmark = pytest.mark.unit

LOCAL_URL = "http://127.0.0.1:8000/v1/chat/completions"


def _overlay_doc() -> dict[str, Any]:
    """A valid overlay: one chain, one local endpoint, one task-kind route set."""
    return {
        "schema_version": "delegation_routing_overlay.v1",
        "escalation_chains": [
            {
                "task_class": "reasoning",
                "escalate_on": "acceptance_check",
                "rungs": ["local"],
            }
        ],
        "local_endpoints": {
            "backends": [
                {
                    "backend_id": "local-coder",
                    "provider": "local",
                    "tier": "local",
                    "endpoint_url": LOCAL_URL,
                    "model_name": "m",
                }
            ]
        },
        "task_kind_rungs": {
            "routes": [
                {
                    "task_class": "reasoning",
                    "task_kind": "ci_red_triage",
                    "local_backend_id": "local-coder",
                    "min_f1": 0.8,
                    "guard": {
                        "min_samples": 20,
                        "accuracy_lower_bound": 0.6,
                        "measured_correct": 123,
                        "measured_n": 165,
                    },
                    "kinds": {
                        "base-red": {
                            "rung": "local",
                            "input": "facts",
                            "n": 20,
                            "f1": 0.92,
                        },
                        "product": {
                            "rung": "current",
                            "input": "facts",
                            "n": 20,
                            "f1": 0.49,
                        },
                    },
                }
            ]
        },
    }


def _route(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["task_kind_rungs"]["routes"][0]


# ---- the valid overlay -----------------------------------------------------


def test_a_valid_overlay_parses_and_the_lookups_answer() -> None:
    overlay = ModelDelegationRoutingOverlay.model_validate(_overlay_doc())
    assert overlay.task_kind_rungs is not None
    route = overlay.task_kind_rungs.route_for("reasoning", "ci_red_triage")
    assert route is not None
    assert route.rung_for("base-red") is EnumTaskKindRung.LOCAL
    assert route.rung_for("product") is EnumTaskKindRung.CURRENT
    assert route.rung_for("no-such-kind") is None
    assert overlay.task_kind_rungs.route_for("reasoning", "no-such-kind") is None
    assert overlay.task_kind_rungs.route_for("no-such-class", "ci_red_triage") is None


# ---- unknown keys (failing controls) ---------------------------------------


def _plant_in_task_kind_rungs(doc: dict[str, Any]) -> None:
    doc["task_kind_rungs"]["bogus"] = True


def _plant_in_route(doc: dict[str, Any]) -> None:
    _route(doc)["bogus"] = True


def _plant_in_kind(doc: dict[str, Any]) -> None:
    _route(doc)["kinds"]["base-red"]["bogus"] = True


def _plant_in_guard(doc: dict[str, Any]) -> None:
    _route(doc)["guard"]["bogus"] = True


@pytest.mark.parametrize(
    "plant",
    [
        pytest.param(_plant_in_task_kind_rungs, id="task_kind_rungs"),
        pytest.param(_plant_in_route, id="route"),
        pytest.param(_plant_in_kind, id="kind-value"),
        pytest.param(_plant_in_guard, id="guard"),
    ],
)
def test_an_unknown_key_in_any_section_is_refused(
    plant: Callable[[dict[str, Any]], None],
) -> None:
    doc = _overlay_doc()
    plant(doc)
    with pytest.raises(ValidationError, match="bogus"):
        ModelDelegationRoutingOverlay.model_validate(doc)


# ---- the min_f1 floor --------------------------------------------------------


def test_a_local_kind_below_min_f1_is_refused_naming_the_kind_and_its_f1() -> None:
    doc = _overlay_doc()
    _route(doc)["kinds"]["base-red"]["f1"] = 0.7
    with pytest.raises(ValidationError, match=r"base-red.*0\.7"):
        ModelDelegationRoutingOverlay.model_validate(doc)


def test_a_current_kind_with_a_low_f1_is_accepted() -> None:
    doc = _overlay_doc()
    overlay = ModelDelegationRoutingOverlay.model_validate(doc)
    route = overlay.task_kind_rungs.route_for("reasoning", "ci_red_triage")
    assert route is not None
    assert route.rung_for("product") is EnumTaskKindRung.CURRENT


# ---- the routing overlay's reference check -----------------------------------


def test_a_route_naming_a_task_class_with_no_chain_is_refused() -> None:
    doc = _overlay_doc()
    _route(doc)["task_class"] = "document"
    with pytest.raises(ValidationError, match="no escalation chain"):
        ModelDelegationRoutingOverlay.model_validate(doc)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda d: _route(d).update(local_backend_id="local-ghost"),
            "local-ghost",
        ),
        (lambda d: d.pop("local_endpoints"), "not declared in local_endpoints"),
    ],
)
def test_a_route_whose_local_backend_is_undeclared_is_refused(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    doc = _overlay_doc()
    mutate(doc)
    with pytest.raises(ValidationError, match=message):
        ModelDelegationRoutingOverlay.model_validate(doc)


def test_a_duplicate_task_class_and_kind_pair_is_refused() -> None:
    doc = _overlay_doc()
    doc["task_kind_rungs"]["routes"].append(copy.deepcopy(_route(doc)))
    with pytest.raises(ValidationError, match="duplicate task_kind route"):
        ModelDelegationRoutingOverlay.model_validate(doc)


# ---- the guard's measurement --------------------------------------------------


def test_measured_correct_above_measured_n_is_refused() -> None:
    doc = _overlay_doc()
    _route(doc)["guard"]["measured_correct"] = 166
    with pytest.raises(ValidationError, match="exceeds"):
        ModelDelegationRoutingOverlay.model_validate(doc)


# ---- an absent section changes nothing ---------------------------------------


def test_an_overlay_without_task_kind_rungs_parses_with_none() -> None:
    doc = _overlay_doc()
    doc.pop("task_kind_rungs")
    overlay = ModelDelegationRoutingOverlay.model_validate(doc)
    assert overlay.task_kind_rungs is None


def test_the_shipped_empty_overlay_has_task_kind_rungs_none() -> None:
    assert EMPTY_DELEGATION_ROUTING_OVERLAY.task_kind_rungs is None
