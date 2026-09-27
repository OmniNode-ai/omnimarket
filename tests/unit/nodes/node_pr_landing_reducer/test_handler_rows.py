# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rows of the revised table the counterexamples do not pin down (OMN-19828, T6).

Each test states one row's observation, target and exact intents, plus the
decisions the reducer takes for inputs the table leaves open, and the new
observation fields' boundary rules.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingAgentReason,
    EnumPrLandingArmMethod,
    EnumPrLandingCompanionStatus,
    EnumPrLandingIntentKind,
    EnumPrLandingObservationKind,
    EnumPrLandingState,
    ModelPrLandingBudgets,
    ModelPrLandingObservation,
    ModelPrLandingState,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers import HandlerPrLandingReducer
from omnimarket.nodes.node_pr_landing_reducer.models import (
    ModelPrLandingReduceInput,
    ModelPrLandingReduceOutput,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from tests.unit.nodes.node_pr_landing_reducer._builders import (
    OCC_PR,
    PR_NUMBER,
    REPOSITORY,
    T0,
    head,
    observation,
    start_row,
)

pytestmark = pytest.mark.unit

_I = EnumPrLandingIntentKind
_S = EnumPrLandingState


def _step(
    row: ModelPrLandingState | None, spec: dict[str, Any]
) -> ModelPrLandingReduceOutput:
    obs = observation(spec, 0)
    return HandlerPrLandingReducer().handle(
        ModelPrLandingReduceInput(state=row, observation=obs)
    )


def _raw(
    kind: EnumPrLandingObservationKind, **fields: Any
) -> ModelPrLandingObservation:
    return ModelPrLandingObservation(
        repository=REPOSITORY,
        pr_number=PR_NUMBER,
        kind=kind,
        observed_at=T0,
        source_topic="test.pr-landing",
        source_event_id="e1",
        **fields,
    )


def _kinds(out: ModelPrLandingReduceOutput) -> list[_I]:
    return [i.kind for i in out.intents]


class TestFirstSight:
    def test_the_first_read_of_a_pr_enters_observed(self) -> None:
        out = _step(None, {"kind": "pushed", "head": "h1", "seq": 1})
        assert out.trigger == "pushed"
        assert out.state.state is _S.OBSERVED
        assert out.state.head_sha == head("h1")
        assert out.state.seq == 1
        assert out.state.state_entry_generation == 1

    def test_an_autobind_prompt_is_not_applied(self) -> None:
        row = start_row({"state": "CHECKS_PENDING", "head": "h1", "seq": 1})
        prompt = _raw(EnumPrLandingObservationKind.PUSHED)
        out = HandlerPrLandingReducer().handle(
            ModelPrLandingReduceInput(state=row, observation=prompt)
        )
        assert str(out.dropped_reason).startswith("prompt")
        assert out.state == row


class TestCompanionRows:
    def _pending(self) -> ModelPrLandingState:
        return start_row(
            {
                "state": "COMPANION_PENDING",
                "head": "h1",
                "seq": 1,
                "companion": {"status": "pending", "command_id": "c1"},
            }
        )

    def test_row_5_derives_and_records_the_command(self) -> None:
        row = start_row({"state": "OBSERVED", "head": "h1", "seq": 1})
        out = _step(row, {"kind": "evaluation", "companion_required": True})
        assert out.trigger == "evaluated_companion_required"
        (derive,) = out.intents
        assert derive.kind is _I.COMPANION_DERIVE
        assert out.state.companion.command_id == derive.command_id
        assert out.state.budgets.derive_left == 1

    def test_row_9_minted_arms_the_companion_and_verifies_the_stamp(self) -> None:
        obs = _raw(
            EnumPrLandingObservationKind.COMPANION_OUTCOME,
            command_id="c1",
            companion_outcome="minted",
            occ_pr=OCC_PR,
            companion_stamped=False,
            companion_armed=False,
        )
        out = HandlerPrLandingReducer().handle(
            ModelPrLandingReduceInput(state=self._pending(), observation=obs)
        )
        assert out.trigger == "companion_minted"
        arm, verify = out.intents
        assert (arm.kind, arm.target_pr) == (_I.GITHUB_ARM, OCC_PR)
        assert verify.kind is _I.COMPANION_VERIFY
        assert out.state.companion.status is EnumPrLandingCompanionStatus.OPEN

    def test_row_10_declined_pages_with_the_reason(self) -> None:
        out = _step(
            self._pending(),
            {"kind": "companion_outcome", "command_id": "c1", "outcome": "DECLINED"},
        )
        assert out.trigger == "companion_declined"
        (agent,) = out.intents
        assert agent.agent_reason is EnumPrLandingAgentReason.COMPANION_DECLINED

    def test_rows_11_and_12_retry_within_the_derive_budget(self) -> None:
        retry = _step(
            self._pending(),
            {"kind": "companion_outcome", "command_id": "c1", "outcome": "ERROR"},
        )
        assert retry.trigger == "companion_error_budget_left"
        assert retry.state.companion.command_id not in (None, "c1")
        spent = self._pending().model_copy(
            update={"budgets": ModelPrLandingBudgets(derive_left=0)}
        )
        out = _step(
            spent, {"kind": "companion_outcome", "command_id": "c1", "outcome": "ERROR"}
        )
        assert out.trigger == "companion_error_budget_spent"
        assert out.intents[0].agent_reason is EnumPrLandingAgentReason.COMPANION_ERROR
        assert out.state.companion.status is EnumPrLandingCompanionStatus.NONE

    def test_rows_17_and_18_and_the_open_row(self) -> None:
        row = start_row(
            {
                "state": "COMPANION_OPEN",
                "head": "h1",
                "seq": 1,
                "companion": {"status": "open"},
            }
        )
        conflicting = _step(row, {"kind": "companion_conflicting"})
        assert conflicting.trigger == "companion_conflicting_budget_left"
        assert _kinds(conflicting) == [_I.COMPANION_REGENERATE]
        closed = _step(row, {"kind": "companion_closed"})
        assert closed.trigger == "companion_closed_unmerged"
        assert _kinds(closed) == [_I.COMPANION_DERIVE]
        spent = row.model_copy(
            update={"budgets": ModelPrLandingBudgets(regenerate_left=0)}
        )
        out = _step(spent, {"kind": "companion_conflicting"})
        assert str(out.dropped_reason).startswith("budget")


class TestVerdictRows:
    def _checks(self, **extra: Any) -> ModelPrLandingState:
        return start_row({"state": "CHECKS_PENDING", "head": "h1", "seq": 1, **extra})

    def test_row_25_enqueues_by_the_live_policy_and_sets_armed(self) -> None:
        obs = _raw(
            EnumPrLandingObservationKind.HEAD_CHECKS,
            head_sha=head("h1"),
            verdict=EnumHeadCheckVerdict.GREEN,
            arm_method=EnumPrLandingArmMethod.QUEUE,
        )
        out = HandlerPrLandingReducer().handle(
            ModelPrLandingReduceInput(state=self._checks(), observation=obs)
        )
        (enqueue,) = out.intents
        assert enqueue.kind is _I.GITHUB_ENQUEUE
        assert enqueue.head_sha == head("h1")
        assert out.state.armed is EnumPrLandingArmMethod.QUEUE

    def test_row_25_a_withheld_arm_gate_arms_nothing(self) -> None:
        obs = _raw(
            EnumPrLandingObservationKind.HEAD_CHECKS,
            head_sha=head("h1"),
            verdict=EnumHeadCheckVerdict.GREEN,
        )
        out = HandlerPrLandingReducer().handle(
            ModelPrLandingReduceInput(state=self._checks(), observation=obs)
        )
        assert (out.trigger, out.intents, out.state.armed) == (
            "verdict_green",
            (),
            None,
        )

    def test_rows_30_and_32_one_rerun_per_check_per_head(self) -> None:
        spec = {
            "kind": "head_checks",
            "head": "h1",
            "verdict": "timed_out",
            "check_attempts": {"ci": 1},
        }
        first = _step(self._checks(), spec)
        assert first.trigger == "verdict_rerunnable_budget_left"
        (rerun,) = first.intents
        assert rerun.check_runs == ("ci",)
        assert [(a.check, a.attempt) for a in first.state.expected_attempts] == [
            ("ci", 2)
        ]
        again = _step(first.state, {**spec, "check_attempts": {"ci": 2}})
        assert again.trigger == "verdict_real_red"
        assert again.intents[0].agent_reason is EnumPrLandingAgentReason.REAL_RED

    def test_rows_31_and_32_update_branch_twice_per_head(self) -> None:
        spec = {"kind": "head_checks", "head": "h1", "verdict": "behind_required"}
        row = self._checks()
        for _ in range(2):
            out = _step(row, spec)
            assert out.trigger == "verdict_update_branch"
            row = out.state
        assert _step(row, spec).trigger == "verdict_real_red"

    def test_row_32_product_failed_never_reruns(self) -> None:
        out = _step(
            self._checks(),
            {"kind": "head_checks", "head": "h1", "verdict": "product_failed"},
        )
        assert out.trigger == "verdict_real_red"
        assert _kinds(out) == [_I.AGENT_NEEDED]

    def test_rows_27_and_33_read_again_after_the_poll_interval(self) -> None:
        out = _step(
            self._checks(), {"kind": "head_checks", "head": "h1", "verdict": "pending"}
        )
        (read,) = out.intents
        assert read.kind is _I.GITHUB_READ_HEAD_CHECKS
        assert read.detail == "after_poll_interval"


class TestArmAndBoundRows:
    def test_rows_34_and_35(self) -> None:
        ready = start_row(
            {"state": "READY", "head": "h1", "seq": 1, "armed": "auto_merge"}
        )
        armed = _step(ready, {"kind": "armed_confirmed", "head": "h1"})
        assert armed.state.state is _S.ARMED
        disarmed = _step(armed.state, {"kind": "disarmed", "head": "h1"})
        assert disarmed.state.state is _S.CHECKS_PENDING
        assert disarmed.state.armed is None

    def test_row_42_names_the_state_the_bound_expired_in(self) -> None:
        row = start_row(
            {"state": "READY", "head": "h1", "seq": 1, "state_entry_generation": 5}
        )
        out = _step(
            row, {"kind": "bound_expired", "episode": 0, "state_entry_generation": 5}
        )
        (agent,) = out.intents
        assert agent.agent_reason is EnumPrLandingAgentReason.STALLED
        assert agent.detail == "READY"

    def test_row_40_a_newer_closed_in_closed_advances_the_key_only(self) -> None:
        row = start_row({"state": "CLOSED", "head": "h1", "seq": 2})
        out = _step(row, {"kind": "closed", "seq": 5})
        assert out.state.state is _S.CLOSED
        assert out.state.source_seq == 5
        assert out.state.episode == row.episode


class TestTheNewObservationFields:
    """The fields the reducer reads, refused at the model boundary when inconsistent."""

    @pytest.mark.parametrize(
        ("kind", "fields"),
        [
            (EnumPrLandingObservationKind.HEAD_CHECKS, {"head_sha": head("h1")}),
            (
                EnumPrLandingObservationKind.HEAD_CHECKS,
                {"head_sha": head("h1"), "verdict": EnumHeadCheckVerdict.TIMED_OUT},
            ),
            (
                EnumPrLandingObservationKind.HEAD_CHECKS,
                {
                    "head_sha": head("h1"),
                    "verdict": EnumHeadCheckVerdict.PRODUCT_FAILED,
                    "rerun_checks": ("ci",),
                },
            ),
            (
                EnumPrLandingObservationKind.PUSHED,
                {"verdict": EnumHeadCheckVerdict.GREEN},
            ),
            (EnumPrLandingObservationKind.COMPANION_OUTCOME, {"command_id": "c1"}),
            (
                EnumPrLandingObservationKind.COMPANION_OUTCOME,
                {"command_id": "c1", "companion_outcome": "minted"},
            ),
            (EnumPrLandingObservationKind.COMPANION_MERGED, {}),
            (EnumPrLandingObservationKind.EVALUATION, {"companion_required": True}),
            (EnumPrLandingObservationKind.MERGED, {"draft": True}),
            (EnumPrLandingObservationKind.CONVERTED_TO_DRAFT, {"draft": False}),
            (EnumPrLandingObservationKind.HOLD_LIFTED, {"held": True}),
        ],
    )
    def test_an_inconsistent_observation_is_refused(
        self, kind: EnumPrLandingObservationKind, fields: dict[str, Any]
    ) -> None:
        with pytest.raises(ValidationError):
            _raw(kind, **fields)

    def test_a_consistent_verdict_is_accepted(self) -> None:
        obs = _raw(
            EnumPrLandingObservationKind.HEAD_CHECKS,
            head_sha=head("h1"),
            verdict=EnumHeadCheckVerdict.CHANGE_CONTROL_STALE,
            rerun_checks=("change-control",),
        )
        assert obs.rerun_checks == ("change-control",)
