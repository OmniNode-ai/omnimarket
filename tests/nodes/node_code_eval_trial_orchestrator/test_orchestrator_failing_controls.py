# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23882 -- failing controls of the orchestrator's own legs (OMN-23627 AC16): each named
injected break turns the property it guards red. A break is a monkeypatch inside the test, so
production code carries no break switch; the property is the same check the golden and close-rule
tests make, run against the broken composition, which must raise AssertionError. The reducer's
own breaks are in ``test_failing_controls.py``."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_code_contract_score_effect.models.model_code_score import (
    ModelCodeScoreRequest,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.handlers.handler_code_eval_trial_orchestrator import (
    HandlerCodeEvalTrialOrchestrator,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration import leg
from omnimarket.nodes.node_code_eval_trial_orchestrator.state_codec import StateIoCodec
from omnimarket.nodes.node_code_eval_trial_reducer.handlers import (
    handler_code_eval_trial_reducer as reducer_module,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    Bus,
    make_bus,
    run_request,
)
from tests.nodes.node_code_eval_trial_orchestrator.test_code_draft_hidden_check_never_sent import (
    golden_bus,
)
from tests.nodes.node_code_eval_trial_orchestrator.test_orchestrator_close_rule import (
    abandoned_payload,
)
from tests.nodes.node_code_eval_trial_orchestrator.test_orchestrator_golden_chain import (
    assert_golden,
    golden,
)

pytestmark = pytest.mark.unit


def assert_each_try_has_its_own_worktree(bus: Bus) -> None:
    ids = [u.work_unit_id for u in bus.units]
    assert len(ids) == len(set(ids)) == 12, "a unit id names a worktree: one per try"
    assert {u.commit_sha for u in bus.units} == {"a" * 40}, (
        "every try starts at the base"
    )


def assert_provenance(bus: Bus) -> None:
    assert bus.units
    for unit in bus.units:
        assert unit.code_draft is not None
        assert unit.code_draft.traffic_class is not None
        assert unit.code_draft.source_surface


def assert_abandoned_terminal_is_built() -> None:
    bus = make_bus(hosts=())
    bus.send(run_request(1, 1))
    built = StateIoCodec().build_abandoned_terminal(**abandoned_payload(bus))
    assert built is not None
    assert built[2]["abandoned"] is True


def red(check, *args) -> None:
    with pytest.raises(AssertionError):
        check(*args)


def test_control_the_unbroken_composition_is_green() -> None:
    bus = golden(redeliver=False)
    assert_golden(bus)
    assert_each_try_has_its_own_worktree(bus)
    assert_provenance(bus)
    assert_abandoned_terminal_is_built()


def test_break_skip_the_score_request(monkeypatch: pytest.MonkeyPatch) -> None:
    real = HandlerCodeEvalTrialOrchestrator.handle

    async def skipping(self, request):
        return [
            e
            for e in await real(self, request)
            if not isinstance(e, ModelCodeScoreRequest)
        ]

    monkeypatch.setattr(HandlerCodeEvalTrialOrchestrator, "handle", skipping)
    red(assert_golden, golden(redeliver=False))


def test_break_drop_build_abandoned_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        StateIoCodec, "build_abandoned_terminal", lambda _self, **_kw: None
    )
    red(assert_abandoned_terminal_is_built)


def test_break_drop_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    real = leg.build_unit_request

    def without(run_id, place, material, overlay):
        unit = real(run_id, place, material, overlay)
        draft = unit.code_draft.model_copy(
            update={"traffic_class": None, "source_surface": None}
        )
        return unit.model_copy(update={"code_draft": draft})

    monkeypatch.setattr(leg, "build_unit_request", without)
    red(assert_provenance, golden_bus())


def test_break_reuse_an_earlier_trys_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    # every try of a sequence gets the same unit id, so the host reuses the first try's worktree
    monkeypatch.setattr(
        reducer_module,
        "unit_id_for",
        lambda _run_id, task_key, model, _ordinal: f"reused-{task_key}-{model}",
    )
    red(assert_each_try_has_its_own_worktree, golden(redeliver=False))


def test_break_drop_the_nudge_expiry_at_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
        ModelLabWorkUnitRequest,
    )

    def check() -> None:
        bus = make_bus(lose=(ModelLabWorkUnitRequest,))
        bus.send(run_request(1, 1))
        bus.drain()
        bus.advance(600 + 60 + 1)
        bus.nudge()
        assert len(bus.units) == 2

    check()  # the control: the nudge places the next try
    monkeypatch.setattr(
        reducer_module.HandlerCodeEvalTrialReducer,
        "_nudge",
        lambda _self, _leg: "nudge_ignored",
    )
    red(check)
