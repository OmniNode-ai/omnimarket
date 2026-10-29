# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21396: remote lanes start cheap and only escalate upward."""

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    EnumRemoteLaneOutcome,
    ModelRemoteLaneAttempt,
    ModelRemoteLaneLadder,
    ModelRemoteLanePlacementRequest,
    ModelRemoteLanePlacementResult,
)

pytestmark = pytest.mark.unit


def place(
    *, ladder: dict[str, Any] | None = None, **request: Any
) -> ModelRemoteLanePlacementResult:
    return HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest.model_validate(
            {
                "engine": "claude_sonnet",
                "readings": [
                    {
                        "name": "h201",
                        "engines": ["claude_sonnet"],
                        "lane_slots": 2,
                        "lane_cap": 4,
                        "rank_free": 10.0,
                    }
                ],
                "ladder": {
                    "lane": "fix-1",
                    "ticket": "OMN-21396",
                    "risk_class": "small-fix",
                    "code_edit_ready": True,
                    "local_model_slots": 1,
                    "ceiling": "sonnet",
                    **(ladder or {}),
                },
                **request,
            }
        )
    )


def test_ac1_cheapest_ready_rung_and_local_slot_fallback() -> None:
    result = place()
    assert (result.rung, result.host, result.engine, result.experiment) == (
        "code-edit",
        "h201",
        "claude_sonnet",
        None,
    )
    result = place(ladder={"local_model_slots": 0})
    assert result.rung == "glm"
    assert [(v.engine, v.reason) for v in result.rungs] == [
        ("code-edit", "no-local-slot"),
        ("glm", "chosen"),
    ]


@pytest.mark.parametrize(("slots", "rung"), [(0, "glm"), (1, "code-edit")])
def test_local_engines_ignore_claude_marks(slots: int, rung: str) -> None:
    result = place(
        ladder={"local_model_slots": slots},
        limited_hosts=["h201"],
        auth_expired_hosts=["h201"],
    )
    assert (result.rung, result.host) == (rung, "h201")


@pytest.mark.parametrize("outcome", ["failed", "blocked-own-work", "checks-red"])
def test_ac2_failed_attempt_escalates_and_emits_experiment(outcome: str) -> None:
    result = place(
        ladder={
            "attempts": [
                {
                    "engine": "code-edit",
                    "model": "qwen",
                    "outcome": outcome,
                    "failure_cause": "checks-red",
                    "duration_s": 120,
                }
            ]
        }
    )
    assert result.rung == "glm"
    assert result.rungs[0].engine == "glm"
    assert result.experiment is not None
    assert (
        result.experiment.attempt,
        result.experiment.engine,
        result.experiment.next_engine,
    ) == (1, "code-edit", "glm")


@pytest.mark.parametrize("outcome", ["done", "blocked-external"])
def test_non_escalating_outcome_stops(outcome: str) -> None:
    result = place(
        ladder={
            "attempts": [{"engine": "code-edit", "model": "qwen", "outcome": outcome}]
        }
    )
    assert (result.rung, result.host, result.outcome, result.rungs) == (
        None,
        None,
        EnumRemoteLaneOutcome.NO_HOST,
        (),
    )
    assert result.experiment is not None
    assert result.experiment.next_engine == "none"


def test_route_ceiling_blocks_higher_rungs() -> None:
    result = place(
        ladder={
            "ceiling": "glm",
            "attempts": [{"engine": "glm", "model": "glm-5.3", "outcome": "failed"}],
        }
    )
    assert (result.rung, result.host) == (None, None)
    assert [(v.engine, v.reason) for v in result.rungs] == [
        (r, "above-route-ceiling") for r in ("codex", "sonnet", "opus")
    ]
    assert result.experiment is not None
    assert result.experiment.next_engine == "none"


def test_claude_required_skips_non_claude_rungs() -> None:
    result = place(ladder={"claude_required": True})
    assert result.rung == "sonnet"
    assert [v.reason for v in result.rungs[:-1]] == ["claude-required"] * 3


@pytest.mark.parametrize(
    "cap",
    ["host-paths-write", "host-process-read", "linear-write", "nested-sandbox"],
)
def test_codex_skips_capabilities_its_sandbox_cannot_grant(cap: str) -> None:
    result = place(
        ladder={
            "ceiling": "sonnet",
            "risk_class": "feature",
            "required_capabilities": (cap,),
        },
        readings=[
            {
                "name": "h201",
                "engines": ["codex", "claude_sonnet"],
                "codex_refusal": None,
                "lane_slots": 2,
                "lane_cap": 4,
                "rank_free": 10.0,
            }
        ],
    )
    assert result.rung == "sonnet"
    assert next(v.reason for v in result.rungs if v.engine == "codex") == (
        "sandbox-capability"
    )


@pytest.mark.parametrize("required_capabilities", [(), ("docker",)])
def test_codex_accepts_other_declared_capabilities(
    required_capabilities: tuple[str, ...],
) -> None:
    result = place(
        ladder={
            "ceiling": "sonnet",
            "risk_class": "feature",
            "required_capabilities": required_capabilities,
        },
        readings=[
            {
                "name": "h201",
                "engines": ["codex", "claude_sonnet"],
                "codex_refusal": None,
                "lane_slots": 2,
                "lane_cap": 4,
                "rank_free": 10.0,
            }
        ],
    )
    assert result.rung == "codex"


def test_failed_sonnet_attempt_is_the_reason_that_admits_opus() -> None:
    result = place(
        ladder={
            "claude_required": True,
            "ceiling": "opus",
            "attempts": [{"engine": "sonnet", "model": "sonnet", "outcome": "failed"}],
        },
        readings=[
            {
                "name": "h201",
                "engines": ["claude_sonnet", "claude_opus"],
                "lane_slots": 2,
            }
        ],
    )
    assert (result.host, result.rung) == ("h201", "opus")
    assert result.engine == "claude_opus"


def test_declared_complexity_reason_admits_opus_on_the_ladder() -> None:
    ladder = {
        "claude_required": True,
        "ceiling": "opus",
        "attempts": [{"engine": "codex", "model": "codex", "outcome": "failed"}],
    }
    readings = [{"name": "h201", "engines": ["claude_opus"], "lane_slots": 2}]
    refused = place(ladder=ladder, readings=readings)
    assert refused.rung is None
    assert "escalation_reason" in refused.rungs[-1].reason
    admitted = place(
        ladder={**ladder, "escalation_reason": "complexity:B4"}, readings=readings
    )
    assert admitted.rung == "opus"


def test_pinned_route_only_uses_ceiling() -> None:
    result = place(
        ladder={"pinned": True, "ceiling": "opus"},
        readings=[{"name": "h201", "engines": ["claude_opus"], "lane_slots": 2}],
    )
    assert (result.host, result.rung) == (None, None)
    assert [v.reason for v in result.rungs[:-1]] == ["pinned-to-route"] * 4
    assert "RULING 2026-10-28T13:32:09Z" in result.rungs[-1].reason


@pytest.mark.parametrize(
    ("ladder", "reason"),
    [
        ({"risk_class": None}, "risk-class"),
        ({"landing_worker": True}, "landing-worker"),
    ],
)
def test_low_risk_and_landing_worker_gates(ladder: dict[str, Any], reason: str) -> None:
    result = place(
        ladder=ladder, readings=[{"name": "h201", "engines": [], "codex_refusal": None}]
    )
    assert result.rung == "codex"
    assert [v.reason for v in result.rungs[:2]] == [reason, reason]


def test_code_edit_requires_writable_globs_and_check() -> None:
    result = place(ladder={"code_edit_ready": False})
    assert result.rung == "glm"
    assert result.rungs[0].reason == "no-writable-or-check"


def test_no_capacity_continues_upward() -> None:
    result = place(readings=[{"name": "h201", "engines": [], "codex_refusal": None}])
    assert result.rung == "codex"
    assert [v.reason for v in result.rungs[:2]] == ["no-capacity: NO_HOST"] * 2


def test_no_rung_preserves_last_venv_drift_and_request_engine() -> None:
    result = place(
        launching_venv_hash="new",
        readings=[
            {
                "name": "h201",
                "engines": ["claude_sonnet"],
                "lane_slots": 2,
                "dispatch_venv_hash": "old",
            }
        ],
    )
    assert (result.rung, result.host, result.engine, result.outcome) == (
        None,
        None,
        "claude_sonnet",
        EnumRemoteLaneOutcome.VENV_DRIFT,
    )
    assert result.venv_drift is not None
    assert result.reconcile is not None
    assert result.reconcile.hosts == ("h201",)


def test_no_ladder_preserves_old_producer() -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(engine="claude_sonnet")
    )
    assert (result.rung, result.rungs) == (None, ())


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (ModelRemoteLaneAttempt, {"engine": "bad", "model": "qwen", "outcome": "done"}),
        (
            ModelRemoteLaneAttempt,
            {"engine": "glm", "model": "glm", "outcome": "done", "duration_s": -1},
        ),
        (ModelRemoteLaneLadder, {"lane": "fix", "ceiling": "bad"}),
        (
            ModelRemoteLaneLadder,
            {"lane": "fix", "ceiling": "glm", "local_model_slots": -1},
        ),
    ],
)
def test_ladder_models_reject_invalid_values(
    model: Any, payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_lane_kind_stands_in_for_an_undeclared_risk_class() -> None:
    result = place(ladder={"risk_class": None, "lane_kind": "small-fix"})
    assert result.rung == "code-edit"
    result = place(ladder={"risk_class": None, "lane_kind": "design-build"})
    assert result.rung == "codex"
    assert [(v.engine, v.reason) for v in result.rungs][:2] == [
        ("code-edit", "risk-class"),
        ("glm", "risk-class"),
    ]


@pytest.mark.parametrize("engine", ["claude_opus", "claude-opus", "opus"])
def test_no_opus_legacy_placement_is_refused_with_ruling(engine: str) -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine=engine,
            readings=[{"name": "h201", "engines": [engine], "lane_slots": 2}],
        )
    )
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.NO_HOST
    assert "RULING 2026-10-28T13:32:09Z" in result.verdicts[0].reason


def test_legacy_placement_carries_the_reason_to_the_policy() -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine="claude_opus",
            escalation_reason="complexity:B5",
            readings=[{"name": "h201", "engines": ["claude_opus"], "lane_slots": 2}],
        )
    )
    assert result.host == "h201"
