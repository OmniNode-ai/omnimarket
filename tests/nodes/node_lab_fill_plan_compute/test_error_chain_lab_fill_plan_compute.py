# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: a request the planner cannot decide is refused, a bad reading is named (OMN-20668)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillCapacity,
    HandlerLabFillDispatchPlan,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCapacityRequest,
    ModelLabFillCapacityResult,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillHeadroomPolicy,
    ModelLabFillPlanConfig,
)

GOOD = {"parent_ticket": "OMN-100", "milestone": "M1", "run_key": "2026-10-03"}


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"parent_ticket": "TICKET-1"}, "parent_ticket must be OMN-<n>"),
        ({"parent_ticket": ""}, "parent_ticket must be OMN-<n>"),
        ({"milestone": "  "}, "milestone must not be blank"),
        ({"run_key": "yesterday"}, "run_key must be YYYY-MM-DD"),
        ({"project_id": "p1"}, "operator_id is required with project_id"),
        ({"max_lanes": 0}, "greater than or equal to 1"),
        ({"max_lanes": 13}, "less than or equal to 12"),
        ({"fallback_max_age_min": 0}, "greater than or equal to 1"),
        ({"unknown_key": 1}, "Extra inputs are not permitted"),
    ],
)
def test_config_refuses_what_the_workflow_refused(
    override: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelLabFillPlanConfig(**{**GOOD, **override})


@pytest.mark.parametrize(
    "override",
    [
        {"max_busy_per_core": 0},
        {"max_busy_per_core": 1.5},
        {"lane_cores": -1},
        {"lane_mem_gb": 0},
        {"max_per_host_per_run": 13},
    ],
)
def test_policy_refuses_non_positive_and_over_cap_values(
    override: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ModelLabFillHeadroomPolicy(**override)


def test_config_normalises_lists_and_keeps_no_identity_of_its_own() -> None:
    config = ModelLabFillPlanConfig(
        **GOOD, pr_repos=(" Repo_A ", ""), exclude_areas=("Billing",)
    )
    assert config.pr_repos == ("repo_a",)
    assert config.exclude_areas == ("billing",)
    assert config.project_id == ""
    assert config.operator_id == ""


def test_requests_refuse_a_wrong_shape() -> None:
    with pytest.raises(ValidationError):
        ModelLabFillCapacityRequest(
            readings="not a list", policy=ModelLabFillHeadroomPolicy()
        )
    with pytest.raises(ValidationError):
        ModelLabFillDispatchPlanRequest(
            candidates=(), capacity={"hosts": []}, config=ModelLabFillPlanConfig(**GOOD)
        )


def test_unreadable_readings_are_named_not_dropped() -> None:
    result = HandlerLabFillCapacity().handle(
        ModelLabFillCapacityRequest(
            readings=(
                None,
                {"name": "host_a", "error": "ssh timeout"},
                {"name": "host_b"},
            ),
            policy=ModelLabFillHeadroomPolicy(),
        )
    )
    assert [(h.name, h.reason, h.lanes) for h in result.hosts] == [
        ("?", "no-reading", 0),
        ("host_a", "unreadable: ssh timeout", 0),
        ("host_b", "incomplete-reading", 0),
    ]
    assert (result.free, result.budget) == (0, 0)


def test_a_malformed_candidate_is_skipped_with_its_reason_and_nothing_dispatches() -> (
    None
):
    result = HandlerLabFillDispatchPlan().handle(
        ModelLabFillDispatchPlanRequest(
            candidates=(
                None,
                "text",
                {"kind": "bogus"},
                {"kind": "ticket", "ticket": "x"},
            ),
            capacity=ModelLabFillCapacityResult(hosts=(), free=0, budget=0),
            config=ModelLabFillPlanConfig(**GOOD),
        )
    )
    assert result.dispatch == ()
    assert result.budget == 0
    assert [s.reason for s in result.skipped] == [
        "malformed",
        "malformed",
        "unknown-kind",
        "no-ticket",
    ]
