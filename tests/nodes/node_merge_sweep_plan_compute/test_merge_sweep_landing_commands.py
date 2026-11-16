# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Landing commands: the watch-only commands a sweep plans for its reds (OMN-20676)."""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_merge_sweep_plan_compute.handlers import (
    HandlerMergeSweepPlan,
)
from omnimarket.nodes.node_merge_sweep_plan_compute.models import (
    ModelMergeSweepPlanRequest,
    ModelMergeSweepPlanResult,
)

_HEAD = "a" * 40
_REPO = "OmniNode-ai/omnimarket"


def _plan(reading: dict[str, object], **extra: object) -> ModelMergeSweepPlanResult:
    request = ModelMergeSweepPlanRequest.model_validate({"reading": reading, **extra})
    return HandlerMergeSweepPlan().handle(request)


def _reading(**extra: object) -> dict[str, object]:
    """A healthy reading whose only lanes come from its rows."""
    base: dict[str, object] = {
        "controller": {"stalled": False},
        "product": {"under_floor": []},
        "load1": 1.0,
        "cpus": 8,
    }
    base.update(extra)
    return base


def test_rerun_cancelled_latest_plans_one_rerun_and_no_fix_lane() -> None:
    """A cancelled-latest red reruns its newest runs once and loses its fix lane."""
    plan = _plan(
        _reading(
            reds=[
                {
                    "repo": "omnimarket",
                    "number": 7,
                    "state": "OPEN",
                    "repository": _REPO,
                    "head_sha": _HEAD,
                    "classes": [
                        {
                            "name": "a",
                            "cls": "cancelled-latest",
                            "remedy": "rerun-newest-once",
                            "run_id": 11,
                        },
                        {
                            "name": "b",
                            "cls": "cancelled-latest",
                            "remedy": "rerun-newest-once",
                            "run_id": 12,
                        },
                    ],
                }
            ]
        )
    )
    assert len(plan.landing_commands) == 1
    cmd = plan.landing_commands[0]
    assert cmd.request.operation == "rerun_runs"
    assert cmd.request.mode is EnumPrLandingGithubMode.DRY_RUN
    assert tuple(cmd.request.run_ids) == (11, 12)
    assert cmd.idempotency_key == f"{_REPO}#7@{_HEAD}:rerun_runs"
    assert plan.lanes == []

    issued = _plan(
        _reading(
            reds=[
                {
                    "repo": "omnimarket",
                    "number": 7,
                    "state": "OPEN",
                    "repository": _REPO,
                    "head_sha": _HEAD,
                    "classes": [
                        {
                            "name": "a",
                            "cls": "cancelled-latest",
                            "remedy": "rerun-newest-once",
                            "run_id": 11,
                        },
                        {
                            "name": "b",
                            "cls": "cancelled-latest",
                            "remedy": "rerun-newest-once",
                            "run_id": 12,
                        },
                    ],
                }
            ],
            issued_commands=[
                {
                    "repo": "omnimarket",
                    "number": 7,
                    "head_sha": _HEAD,
                    "operation": "rerun_runs",
                }
            ],
        )
    )
    assert issued.landing_commands == []
    assert len(issued.landing_skipped) == 1
    assert issued.landing_skipped[0].why.startswith("issued-at-head")
    assert [lane.kind for lane in issued.lanes] == ["fix"]


def test_pace_one_update_branch_per_base() -> None:
    """One update_branch per (repository, base_ref); the later PR keeps its fix lane."""
    reds = [
        {
            "repo": "omnimarket",
            "number": 3,
            "state": "OPEN",
            "repository": _REPO,
            "head_sha": _HEAD,
            "base_ref": "dev",
            "classes": [
                {"name": "a", "cls": "stale-base-sql", "remedy": "update-branch"},
                {"name": "b", "cls": "draft-era", "remedy": "update-branch"},
            ],
        },
        {
            "repo": "omnimarket",
            "number": 4,
            "state": "OPEN",
            "repository": _REPO,
            "head_sha": _HEAD,
            "base_ref": "dev",
            "classes": [
                {"name": "a", "cls": "stale-base-sql", "remedy": "update-branch"},
                {"name": "b", "cls": "draft-era", "remedy": "update-branch"},
            ],
        },
        {
            "repo": "omnimarket",
            "number": 5,
            "state": "OPEN",
            "repository": _REPO,
            "head_sha": _HEAD,
            "base_ref": "main",
            "classes": [
                {"name": "a", "cls": "stale-base-sql", "remedy": "update-branch"},
            ],
        },
    ]
    plan = _plan(_reading(reds=reds))
    assert [c.request.pr_number for c in plan.landing_commands] == [3, 5]
    assert all(c.request.operation == "update_branch" for c in plan.landing_commands)
    assert [(s.pr, s.why) for s in plan.landing_skipped] == [
        ("omnimarket#4", "paced-base update_branch base=dev")
    ]
    fix = [lane for lane in plan.lanes if lane.kind == "fix"]
    assert [p.pr for p in fix[0].prs] == ["omnimarket#4"]


def test_arm_green_unarmed_chain_head_and_workflow_scope_arm() -> None:
    """A green-unarmed chain head and a workflow-scope-arm red arm; a queue base enqueues."""
    plan = _plan(
        _reading(
            chain_heads=[
                {
                    "repo": "omnimarket",
                    "number": 10,
                    "state": "OPEN",
                    "repository": _REPO,
                    "head_sha": _HEAD,
                    "pr_node_id": "node-10",
                    "cls": "green-unarmed",
                }
            ],
            reds=[
                {
                    "repo": "omnimarket",
                    "number": 11,
                    "state": "OPEN",
                    "repository": _REPO,
                    "head_sha": _HEAD,
                    "pr_node_id": "node-11",
                    "merge_queue": False,
                    "classes": [
                        {
                            "name": "a",
                            "cls": "workflow-scope-arm",
                            "remedy": "merge-on-head-without-arming",
                        },
                    ],
                },
                {
                    "repo": "omnimarket",
                    "number": 12,
                    "state": "OPEN",
                    "repository": _REPO,
                    "head_sha": _HEAD,
                    "pr_node_id": "node-12",
                    "merge_queue": True,
                    "classes": [
                        {
                            "name": "a",
                            "cls": "workflow-scope-arm",
                            "remedy": "merge-on-head-without-arming",
                        },
                    ],
                },
            ],
        )
    )
    assert [c.request.operation for c in plan.landing_commands] == [
        "arm_auto_merge",
        "arm_auto_merge",
        "enqueue",
    ]
    assert [c.request.pr_number for c in plan.landing_commands] == [10, 11, 12]
    assert plan.lanes == []


def test_wait_in_progress_plans_nothing() -> None:
    """An in-progress red plans no command and keeps its fix lane."""
    plan = _plan(
        _reading(
            reds=[
                {
                    "repo": "omnimarket",
                    "number": 13,
                    "state": "OPEN",
                    "repository": _REPO,
                    "head_sha": _HEAD,
                    "classes": [{"name": "a", "cls": "in-progress", "remedy": "wait"}],
                }
            ]
        )
    )
    assert plan.landing_commands == []
    assert plan.landing_skipped == []
    assert [lane.kind for lane in plan.lanes] == ["fix"]


def test_unread_head_or_run_ids_are_skipped_with_reason_unread() -> None:
    """A missing fact is skipped with its reason, never guessed, and keeps its lane."""
    no_head = _plan(
        _reading(
            reds=[
                {
                    "repo": "omnimarket",
                    "number": 14,
                    "state": "OPEN",
                    "repository": _REPO,
                    "classes": [
                        {
                            "name": "a",
                            "cls": "cancelled-latest",
                            "remedy": "rerun-newest-once",
                            "run_id": 11,
                        },
                    ],
                }
            ]
        )
    )
    assert no_head.landing_commands == []
    assert no_head.landing_skipped[0].why.startswith("unread")
    assert [lane.kind for lane in no_head.lanes] == ["fix"]

    no_run = _plan(
        _reading(
            reds=[
                {
                    "repo": "omnimarket",
                    "number": 15,
                    "state": "OPEN",
                    "repository": _REPO,
                    "head_sha": _HEAD,
                    "classes": [
                        {
                            "name": "a",
                            "cls": "cancelled-latest",
                            "remedy": "rerun-newest-once",
                        },
                    ],
                }
            ]
        )
    )
    assert no_run.landing_commands == []
    assert no_run.landing_skipped[0].why.startswith("unread")
    assert [lane.kind for lane in no_run.lanes] == ["fix"]

    no_node = _plan(
        _reading(
            reds=[
                {
                    "repo": "omnimarket",
                    "number": 16,
                    "state": "OPEN",
                    "repository": _REPO,
                    "head_sha": _HEAD,
                    "classes": [
                        {
                            "name": "a",
                            "cls": "workflow-scope-arm",
                            "remedy": "merge-on-head-without-arming",
                        },
                    ],
                }
            ]
        )
    )
    assert no_node.landing_commands == []
    assert no_node.landing_skipped[0].why.startswith("unread")
    assert [lane.kind for lane in no_node.lanes] == ["fix"]


def test_real_failure_names_the_failure_record_and_plans_no_command() -> None:
    """A real failure is named in the skip record and keeps its fix lane."""
    plan = _plan(
        _reading(
            reds=[
                {
                    "repo": "omnimarket",
                    "number": 17,
                    "state": "OPEN",
                    "repository": _REPO,
                    "head_sha": _HEAD,
                    "classes": [{"name": "a", "cls": "real", "remedy": "fix"}],
                }
            ]
        )
    )
    assert plan.landing_commands == []
    assert [(s.pr, s.why) for s in plan.landing_skipped] == [
        ("omnimarket#17", "real failure-record=OMN-23832")
    ]
    assert [lane.kind for lane in plan.lanes] == ["fix"]


def test_every_command_validates_as_the_landing_effects_request() -> None:
    """Every command round-trips as a landing request; the plan is deterministic."""
    reading = _reading(
        reds=[
            {
                "repo": "omnimarket",
                "number": 20,
                "state": "OPEN",
                "repository": _REPO,
                "head_sha": _HEAD,
                "base_ref": "dev",
                "classes": [
                    {"name": "a", "cls": "stale-base-sql", "remedy": "update-branch"},
                ],
            },
            {
                "repo": "omnimarket",
                "number": 21,
                "state": "OPEN",
                "repository": _REPO,
                "head_sha": _HEAD,
                "classes": [
                    {
                        "name": "a",
                        "cls": "cancelled-latest",
                        "remedy": "rerun-newest-once",
                        "run_id": 30,
                    },
                ],
            },
        ]
    )
    plan = _plan(reading)
    for cmd in plan.landing_commands:
        round_trip = ModelPrLandingGithubRequest.model_validate(
            cmd.request.model_dump()
        )
        assert round_trip == cmd.request
        assert cmd.request.mode is EnumPrLandingGithubMode.DRY_RUN

    again = _plan(reading)
    assert [
        (c.idempotency_key, c.request.model_dump()) for c in plan.landing_commands
    ] == [(c.idempotency_key, c.request.model_dump()) for c in again.landing_commands]

    run_id = UUID("00000000-0000-0000-0000-000000000001")
    tagged = _plan(reading, correlation_id=run_id)
    assert tagged.landing_commands
    for cmd in tagged.landing_commands:
        assert cmd.request.correlation_id == run_id


def test_a_command_free_reading_plans_the_same_lanes_as_before() -> None:
    """A red without a head plans its fix lane and no command (today's behaviour)."""
    plan = _plan(
        _reading(
            reds=[
                {
                    "repo": "omnimarket",
                    "number": 22,
                    "state": "OPEN",
                    "classes": [
                        {
                            "name": "a",
                            "cls": "cancelled-latest",
                            "remedy": "rerun-newest-once",
                            "run_id": 40,
                        },
                    ],
                }
            ]
        )
    )
    assert [lane.kind for lane in plan.lanes] == ["fix"]
    assert plan.landing_commands == []


def _stale_red(number: int, base_ref: str, **extra: object) -> dict[str, object]:
    return {
        "repo": "omnimarket",
        "number": number,
        "state": "OPEN",
        "repository": _REPO,
        "head_sha": _HEAD,
        "base_ref": base_ref,
        "classes": [{"name": "a", "cls": "stale-base-sql", "remedy": "update-branch"}],
        **extra,
    }


def test_an_update_branch_red_that_carries_a_node_id_still_validates() -> None:
    """The node id belongs to arm and enqueue only: an update_branch never carries it."""
    plan = _plan(_reading(reds=[_stale_red(6, "dev", pr_node_id="node-6")]))
    assert [c.request.operation for c in plan.landing_commands] == ["update_branch"]
    assert plan.landing_commands[0].request.pr_node_id is None


def test_a_real_failure_beside_a_command_keeps_its_fix_lane() -> None:
    """A real class is never answered by a landing command: the PR keeps its fix lane too."""
    plan = _plan(
        _reading(
            reds=[
                _stale_red(
                    8,
                    "dev",
                    classes=[
                        {
                            "name": "a",
                            "cls": "stale-base-sql",
                            "remedy": "update-branch",
                        },
                        {"name": "b", "cls": "real", "remedy": "fix"},
                    ],
                )
            ]
        )
    )
    assert [c.request.operation for c in plan.landing_commands] == ["update_branch"]
    assert [s.why for s in plan.landing_skipped] == ["real failure-record=OMN-23832"]
    assert [lane.kind for lane in plan.lanes] == ["fix"]


def test_a_broken_pace_fails_the_pace_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: a base that always takes its update lets both reds on one base update."""
    from omnimarket.nodes.node_merge_sweep_plan_compute.handlers import (
        handler_merge_sweep_plan,
    )

    reading = _reading(reds=[_stale_red(3, "dev"), _stale_red(4, "dev")])
    assert [c.request.pr_number for c in _plan(reading).landing_commands] == [3]
    monkeypatch.setattr(
        handler_merge_sweep_plan, "_base_takes_update", lambda _paced, _base: True
    )
    assert [c.request.pr_number for c in _plan(reading).landing_commands] == [3, 4]
