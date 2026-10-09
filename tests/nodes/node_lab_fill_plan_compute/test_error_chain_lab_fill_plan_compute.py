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


@pytest.mark.parametrize(
    ("op", "key", "bad"),
    [
        ("decide_lab_fill_ownership", "claim_index", {"OMN-7": {"state": "held"}}),
        (
            "decide_lab_fill_ownership",
            "ledger_claims",
            [{"lane": "peer", "subject": "OMN-7", "when": "not a time"}],
        ),
        ("decide_lab_fill_ownership", "now", "not a time"),
        ("render_lab_fill_lane", "item", {"lane": "invalid"}),
        ("render_lab_fill_lane", "config", {"authority_ruling": "missing"}),
        ("verify_lab_fill_placement", "receipts", ["bad reading"]),
        ("plan_lab_fill_fallbacks", "capacity", {"hosts": ["bad reading"]}),
        (
            "compose_lab_fill_status",
            "candidates",
            {"source": "enumerate", "candidates": [None]},
        ),
        (
            "compose_lab_fill_status",
            "candidates",
            {"source": "enumerate", "source_diagnostics": {"ticket": "broken"}},
        ),
    ],
)
def test_new_contract_bindings_refuse_malformed_shapes(
    op: str, key: str, bad: object
) -> None:
    """Validation identifies the bad input before any handler can dereference it."""
    from .test_lab_fill_outcome_parity import CASES, OPERATIONS

    handler_type, request_type, _ = OPERATIONS[op]
    inputs = next(c["input"] for c in CASES if c["op"] == op)
    with pytest.raises(ValidationError, match=key):
        handler_type().handle(request_type.model_validate({**inputs, key: bad}))


def test_new_handlers_name_unread_ownership_and_missing_receipts() -> None:
    """Reader failures remain visible in the pure result, including malformed scalar values."""
    from .test_lab_fill_outcome_parity import CASES, OPERATIONS

    op = "decide_lab_fill_ownership"
    handler_type, request_type, _ = OPERATIONS[op]
    inputs = next(c["input"] for c in CASES if c["name"] == "ownership-free")
    result = handler_type().handle(
        request_type.model_validate({**inputs, "claim_index": "IOError: failed|read"})
    )
    assert result.verdicts[0].skipped == "owned-unreadable"
    assert "claim-index=unreadable:IOError: failed/read" in result.verdicts[0].detail
    op = "verify_lab_fill_placement"
    handler_type, request_type, _ = OPERATIONS[op]
    inputs = next(c["input"] for c in CASES if c["name"] == "placement-missing-receipt")
    result = handler_type().handle(
        request_type.model_validate(
            {
                **inputs,
                "receipts": [None, {"lane": "lane-a", "found": True, "status": {}}],
            }
        )
    )
    assert result.placements[0]["outcome"] == "pending"
    assert result.placements[0]["detail"] == "status=[object Object]"


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("pillar", "INVALID"),
        ("parent_lane", "lane space"),
        ("parent_ticket", "ABC-1"),
        ("authority_ruling", "2026-09-30"),
        ("authority_lane", ""),
        ("landing_lane", "bad/lane"),
        ("lane_timeout_min", 0),
        ("lane_timeout_min", "NaN"),
        ("stalled_hours", -1),
        ("stalled_hours", 1.5),
    ],
)
def test_render_config_refuses_invalid_authority_and_limits(
    field: str, bad: object
) -> None:
    """A malformed ruling or timeout cannot silently produce an unauthorized launch."""
    from omnimarket.nodes.node_lab_fill_plan_compute.models import (
        ModelLabFillRenderConfig,
    )

    from .test_lab_fill_outcome_parity import CASES

    config = next(
        c["input"]["config"] for c in CASES if c["op"] == "render_lab_fill_lane"
    )
    with pytest.raises(ValidationError, match=field):
        ModelLabFillRenderConfig.model_validate({**config, field: bad})
