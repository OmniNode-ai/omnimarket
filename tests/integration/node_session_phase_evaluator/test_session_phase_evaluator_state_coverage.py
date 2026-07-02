# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-verdict coverage for node_session_phase_evaluator (OMN-13674).

COMPUTE archetype -> Variant B: the pure evaluator handler is registered on the
canonical in-memory bus (``EventBusInmemory`` via ``integration_event_bus``)
through ``LocalRuntimeBusAdapter`` (``drive_round_trip``). One evaluation
request is published on the ``session-phase-evaluate.v1`` subscribe topic and
the returned ``ModelPhaseEvaluation`` republished on the
``session-phase-evaluated.v1`` topic is asserted.

The contract declares the ``action`` output enum
``[no_action, budget_warning, transition_required, halt_required]``. This suite
reaches every declared verdict class and exercises every decision branch +
flag:

  * ``no_action``          — within budget, exit conditions unmet,
  * ``budget_warning``     — elapsed >= 80% (warn) but < halt threshold,
  * ``transition_required`` — all exit conditions satisfied (primary edge) AND
    the budget-exhausted edge (elapsed >= 100% under a >100 halt threshold),
  * ``halt_required``      — elapsed >= halt threshold, at BOTH the default
    100% threshold and a custom ``halt_threshold_pct`` flag value,
  * priority: halt out-ranks satisfied exit conditions, and
  * NEGATIVE CONTROL: a known-bad fixture (a phase 20% past its budget) MUST
    yield ``halt_required`` with the elapsed pct clamped to 100 — asserted as
    the concrete verdict, never a "returned without raising" check.

No live Kafka / .201 — fully in-process.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_session_phase_evaluator.handlers.handler_session_phase_evaluator import (
    HandlerSessionPhaseEvaluator,
    ModelPhaseEvaluationRequest,
)
from tests.integration._wave7_bus import drive_round_trip

_START_TOPIC = "onex.cmd.omnimarket.session-phase-evaluate.v1"
_RESULT_TOPIC = "onex.evt.omnimarket.session-phase-evaluated.v1"


def _request(
    *,
    phase_name: str = "merge",
    max_duration_minutes: int = 10,
    elapsed_minutes: float = 0.0,
    exit_condition_statuses: dict[str, bool] | None = None,
    halt_threshold_pct: int = 100,
) -> ModelPhaseEvaluationRequest:
    return ModelPhaseEvaluationRequest(
        phase_name=phase_name,
        max_duration_minutes=max_duration_minutes,
        elapsed_minutes=elapsed_minutes,
        exit_condition_statuses=exit_condition_statuses or {"pr_merged": False},
        halt_threshold_pct=halt_threshold_pct,
    )


async def _verdict(
    request: ModelPhaseEvaluationRequest, bus: Any, *, group: str
) -> dict[str, Any]:
    history = await drive_round_trip(
        bus,
        handler=HandlerSessionPhaseEvaluator(),
        handler_name="session-phase-evaluator",
        input_model_cls=ModelPhaseEvaluationRequest,
        start_topic=f"{_START_TOPIC}.{group}",
        output_topic=f"{_RESULT_TOPIC}.{group}",
        payload_bytes=request.model_dump_json().encode("utf-8"),
        group_id=group,
    )
    assert len(history) == 1, "expected exactly one evaluation verdict"
    verdict: dict[str, Any] = json.loads(history[0].value)
    return verdict


# ---------------------------------------------------------------------------
# Every declared verdict class reached.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestEvaluatorDeclaredVerdictCoverage:
    async def test_no_action_when_within_budget_and_conditions_unmet(
        self, integration_event_bus: Any
    ) -> None:
        verdict = await _verdict(
            _request(elapsed_minutes=4.0),  # 40%
            integration_event_bus,
            group="eval-noaction",
        )
        assert verdict["action"] == "no_action"
        assert verdict["budget_elapsed_pct"] == 40

    async def test_budget_warning_at_80_pct(self, integration_event_bus: Any) -> None:
        verdict = await _verdict(
            _request(elapsed_minutes=8.0),  # 80%
            integration_event_bus,
            group="eval-warning",
        )
        assert verdict["action"] == "budget_warning"
        assert verdict["budget_elapsed_pct"] == 80

    async def test_transition_required_when_all_conditions_met(
        self, integration_event_bus: Any
    ) -> None:
        verdict = await _verdict(
            _request(
                elapsed_minutes=5.0,  # 50% — well within budget
                exit_condition_statuses={"pr_merged": True, "ci_green": True},
            ),
            integration_event_bus,
            group="eval-transition-conditions",
        )
        assert verdict["action"] == "transition_required"
        assert verdict["budget_elapsed_pct"] == 50

    async def test_transition_required_when_budget_exhausted(
        self, integration_event_bus: Any
    ) -> None:
        """Budget-exhausted transition edge: elapsed pins at 100% but the halt
        threshold is above 100, so the verdict is transition (not halt)."""
        verdict = await _verdict(
            _request(
                elapsed_minutes=10.0,  # 100%
                halt_threshold_pct=101,
            ),
            integration_event_bus,
            group="eval-transition-budget",
        )
        assert verdict["action"] == "transition_required"
        assert verdict["budget_elapsed_pct"] == 100

    async def test_halt_required_at_default_threshold(
        self, integration_event_bus: Any
    ) -> None:
        verdict = await _verdict(
            _request(elapsed_minutes=10.0),  # 100% == default halt threshold
            integration_event_bus,
            group="eval-halt-default",
        )
        assert verdict["action"] == "halt_required"
        assert verdict["budget_elapsed_pct"] == 100

    async def test_halt_required_at_custom_threshold_flag(
        self, integration_event_bus: Any
    ) -> None:
        """The ``halt_threshold_pct`` flag lowers the halt point below 100%."""
        verdict = await _verdict(
            _request(elapsed_minutes=6.0, halt_threshold_pct=50),  # 60% >= 50
            integration_event_bus,
            group="eval-halt-custom",
        )
        assert verdict["action"] == "halt_required"
        assert verdict["budget_elapsed_pct"] == 60


# ---------------------------------------------------------------------------
# Priority + negative control (known-bad fixture MUST produce the finding).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestEvaluatorDimensions:
    async def test_halt_out_ranks_satisfied_exit_conditions(
        self, integration_event_bus: Any
    ) -> None:
        """A halt-threshold breach wins even when all exit conditions are met."""
        verdict = await _verdict(
            _request(
                elapsed_minutes=10.0,  # 100% -> halt
                exit_condition_statuses={"pr_merged": True},
            ),
            integration_event_bus,
            group="eval-priority",
        )
        assert verdict["action"] == "halt_required"

    async def test_known_bad_overbudget_fixture_must_halt(
        self, integration_event_bus: Any
    ) -> None:
        """NEGATIVE CONTROL: a phase 20% past its budget is a known-bad fixture.

        It MUST produce ``halt_required`` (the finding), NOT ``no_action`` — a
        silent pass here would mean the evaluator failed to detect a blown
        budget. The elapsed pct is clamped to 100.
        """
        verdict = await _verdict(
            _request(
                max_duration_minutes=10,
                elapsed_minutes=12.0,  # 120% of budget -> clamps to 100
            ),
            integration_event_bus,
            group="eval-negctrl",
        )
        assert verdict["action"] == "halt_required"
        assert verdict["action"] != "no_action"
        assert verdict["budget_elapsed_pct"] == 100
