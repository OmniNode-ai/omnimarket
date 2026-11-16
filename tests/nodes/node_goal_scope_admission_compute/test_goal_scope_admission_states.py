# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23851: the goal-scope admission contract declares and fills exactly its three states."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import omnimarket.nodes.node_goal_scope_admission_compute as node_package
from omnimarket.models.goal_scope_admission import (
    ModelGoalScopeAdmissionRequest,
    ModelGoalScopeAdmissionResult,
)
from omnimarket.nodes.node_goal_scope_admission_compute.handlers.handler_goal_scope_admission import (
    HandlerGoalScopeAdmission,
)
from omnimarket.nodes.node_goal_scope_admission_compute.tests.support import (
    CASES,
    request_of,
)

pytestmark = pytest.mark.unit


def _contract() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.safe_load(
            (Path(node_package.__file__).parent / "contract.yaml").read_text(
                encoding="utf-8"
            )
        ),
    )


def _case(name: str) -> dict[str, Any]:
    return next(case for case in CASES if case["name"] == name)


def test_declared_outputs_are_exactly_the_result_fields() -> None:
    contract = _contract()
    assert set(contract["outputs"]) == set(ModelGoalScopeAdmissionResult.model_fields)
    assert set(contract["outputs"]) == {"admitted", "not_admitted", "unread"}


def test_terminal_event_is_the_one_published_topic() -> None:
    contract = _contract()
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.goal-scope-admission-completed.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.goal-scope-admission-completed.v1"
    ]


def test_every_declared_output_is_filled_by_a_real_run() -> None:
    handler = HandlerGoalScopeAdmission()
    found = handler.handle(
        request_of(_case("rebuild_pieces_and_one_runtime_two_roots"))
    )
    assert found.model_dump()["admitted"]
    none_declared = handler.handle(
        ModelGoalScopeAdmissionRequest.model_validate(
            {
                "candidates": {
                    "repo#1": {"title": "a change that names no ticket", "body": ""}
                }
            }
        )
    )
    assert none_declared.model_dump()["not_admitted"] == {
        "repo#1": "declares no ticket"
    }
    assert none_declared.model_dump()["admitted"] == {}
    all_unread = handler.handle(
        request_of(_case("unreadable_linear_holds_everything_unread"))
    )
    assert all_unread.model_dump()["unread"]
    assert all_unread.model_dump()["admitted"] == {}
