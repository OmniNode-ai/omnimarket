# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plan review ladder compute: the next move of a plan's review ladder.

The golden cases walk the ladder: the first whole-plan round while none has
run, whole-plan rounds held open only by an open blocking finding (a kind
outside blocking_kinds never blocks), the handover to the first slice once no
blocking finding is open, first-slice rounds held open by any open finding,
the build of a clean first slice with the next pending rung named, a rung
round repeated for its own rung or advanced to the next pending one, and the
plan completing once no rung is left. The settings come from the request, the
handler's injected settings or the overlay, in that order.

Three injected breaks show the assertions can fail for real: a judge that
counts every open finding as blocking, a judge that ignores the first-slice
verdict, and a judge that never stops the whole-plan review.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.nodes.node_plan_first_slice_compute.models.model_plan_first_slice import (
    EnumPlanFirstSliceVerdict,
)
from omnimarket.nodes.node_plan_review_ladder_compute import HandlerPlanReviewLadder
from omnimarket.nodes.node_plan_review_ladder_compute.handlers import (
    handler_plan_review_ladder,
)
from omnimarket.nodes.node_plan_review_ladder_compute.models.model_plan_review_ladder import (
    EnumPlanReviewMove,
    EnumPlanReviewScope,
    ModelPlanReviewFinding,
    ModelPlanReviewLadderRequest,
    ModelPlanReviewLadderResult,
    ModelPlanReviewLadderSettings,
    ModelPlanReviewRound,
)
from omnimarket.nodes.node_plan_review_ladder_compute.overlay import (
    NODE_NAME,
    load_settings,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

NODE_DIR = Path(handler_plan_review_ladder.__file__).resolve().parents[1]

BLOCKING_KINDS = ("wrong_fact", "cannot_build_or_test", "contradicts_ruling")

SETTINGS = ModelPlanReviewLadderSettings(blocking_kinds=BLOCKING_KINDS)
OTHER_SETTINGS = ModelPlanReviewLadderSettings(blocking_kinds=("style",))


def _finding(kind: str, is_open: bool = True) -> ModelPlanReviewFinding:
    return ModelPlanReviewFinding(kind=kind, open=is_open)


def _round(
    scope: EnumPlanReviewScope,
    findings: tuple[ModelPlanReviewFinding, ...] = (),
    rung: str | None = None,
) -> ModelPlanReviewRound:
    return ModelPlanReviewRound(scope=scope, findings=findings, rung=rung)


CLEAN_FULL_PLAN_ROUNDS = (
    _round(EnumPlanReviewScope.FULL_PLAN, (_finding("wrong_fact", False),)),
)


def _request(
    verdict: EnumPlanFirstSliceVerdict = EnumPlanFirstSliceVerdict.OK,
    rounds: tuple[ModelPlanReviewRound, ...] = (),
    later_rungs: tuple[str, ...] = (),
    settings: ModelPlanReviewLadderSettings | None = SETTINGS,
) -> ModelPlanReviewLadderRequest:
    return ModelPlanReviewLadderRequest(
        first_slice_verdict=verdict,
        rounds=rounds,
        later_rungs=later_rungs,
        settings=settings,
    )


def _expect(
    request: ModelPlanReviewLadderRequest, move: EnumPlanReviewMove
) -> ModelPlanReviewLadderResult:
    result = HandlerPlanReviewLadder().handle(request)
    assert result.move is move, result.detail
    return result


# ---------------------------------------------------------------------------------------------- golden


def test_golden_a_refused_first_slice_returns_to_author() -> None:
    result = _expect(
        _request(verdict=EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION),
        EnumPlanReviewMove.RETURN_TO_AUTHOR,
    )
    assert result.rule == "first_slice_not_ok"
    assert result.next_scope is None
    assert result.next_rung is None
    assert result.open_blocking == 0


def test_golden_no_round_yet_starts_the_first_full_plan_round() -> None:
    result = _expect(_request(), EnumPlanReviewMove.FULL_PLAN_ROUND)
    assert result.rule == "no_round_yet"
    assert result.next_scope is EnumPlanReviewScope.FULL_PLAN
    assert result.next_rung is None
    assert result.open_blocking == 0


def test_golden_an_open_blocking_finding_holds_the_whole_plan_open() -> None:
    rounds = (
        _round(
            EnumPlanReviewScope.FULL_PLAN,
            (
                _finding("wrong_fact"),
                _finding("cannot_build_or_test"),
                _finding("contradicts_ruling", False),
                _finding("style"),
            ),
        ),
    )
    result = _expect(_request(rounds=rounds), EnumPlanReviewMove.FULL_PLAN_ROUND)
    assert result.rule == "blocking_open"
    assert result.open_blocking == 2, "only the open blocking kinds count"
    assert result.next_scope is EnumPlanReviewScope.FULL_PLAN


def test_golden_no_blocking_left_hands_over_to_the_first_slice() -> None:
    result = _expect(
        _request(rounds=CLEAN_FULL_PLAN_ROUNDS),
        EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE,
    )
    assert result.rule == "no_blocking_left"
    assert result.next_scope is EnumPlanReviewScope.FIRST_SLICE
    assert result.next_rung is None
    assert result.open_blocking == 0


def test_golden_a_non_blocking_open_finding_does_not_start_another_round() -> None:
    rounds = (_round(EnumPlanReviewScope.FULL_PLAN, (_finding("style"),)),)
    result = _expect(
        _request(rounds=rounds), EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE
    )
    assert result.rule == "no_blocking_left"
    assert result.open_blocking == 0, "an open style finding is not blocking"


def test_golden_a_finding_kind_outside_blocking_kinds_is_non_blocking() -> None:
    rounds = (
        _round(
            EnumPlanReviewScope.FULL_PLAN,
            (_finding("nitpick"), _finding("cannot_build_or_test", False)),
        ),
    )
    result = _expect(
        _request(rounds=rounds), EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE
    )
    assert result.rule == "no_blocking_left"
    assert result.open_blocking == 0, "nitpick is outside blocking_kinds"


def test_golden_an_open_finding_keeps_the_first_slice_under_review() -> None:
    rounds = (
        CLEAN_FULL_PLAN_ROUNDS[0],
        _round(EnumPlanReviewScope.FIRST_SLICE, (_finding("nitpick"),)),
    )
    result = _expect(_request(rounds=rounds), EnumPlanReviewMove.FIRST_SLICE_ROUND)
    assert result.rule == "first_slice_open"
    assert result.next_scope is EnumPlanReviewScope.FIRST_SLICE
    assert result.next_rung is None


def test_golden_a_clean_first_slice_builds_it_and_names_the_next_rung() -> None:
    rounds = (
        CLEAN_FULL_PLAN_ROUNDS[0],
        _round(EnumPlanReviewScope.FIRST_SLICE, ()),
    )
    result = _expect(
        _request(rounds=rounds, later_rungs=("rung-a", "rung-b")),
        EnumPlanReviewMove.BUILD_FIRST_SLICE,
    )
    assert result.rule == "first_slice_clean"
    assert result.next_scope is None
    assert result.next_rung == "rung-a", "the first pending rung is next"
    without_rungs = _expect(
        _request(rounds=rounds), EnumPlanReviewMove.BUILD_FIRST_SLICE
    )
    assert without_rungs.next_rung is None, "no later rungs, no rung is next"


def test_golden_a_rung_with_an_open_finding_repeats_for_that_rung() -> None:
    rounds = (
        CLEAN_FULL_PLAN_ROUNDS[0],
        _round(EnumPlanReviewScope.FIRST_SLICE, ()),
        _round(EnumPlanReviewScope.LATER_RUNG, (_finding("nitpick"),), rung="rung-a"),
    )
    result = _expect(
        _request(rounds=rounds, later_rungs=("rung-a",)),
        EnumPlanReviewMove.RUNG_ROUND,
    )
    assert result.rule == "rung_open"
    assert result.next_scope is EnumPlanReviewScope.LATER_RUNG
    assert result.next_rung == "rung-a"
    assert result.open_blocking == 0, "any open finding holds the rung"


def test_golden_a_clean_rung_moves_to_the_next_pending_rung() -> None:
    rounds = (
        CLEAN_FULL_PLAN_ROUNDS[0],
        _round(EnumPlanReviewScope.FIRST_SLICE, ()),
        # rung-a's earlier round was open; its LATEST round is the clean one.
        _round(EnumPlanReviewScope.LATER_RUNG, (_finding("nitpick"),), rung="rung-a"),
        _round(
            EnumPlanReviewScope.LATER_RUNG, (_finding("nitpick", False),), rung="rung-a"
        ),
    )
    result = _expect(
        _request(rounds=rounds, later_rungs=("rung-a", "rung-b")),
        EnumPlanReviewMove.RUNG_ROUND,
    )
    assert result.rule == "next_rung"
    assert result.next_scope is EnumPlanReviewScope.LATER_RUNG
    assert result.next_rung == "rung-b", "the clean rung is done, rung-b is next"
    assert result.open_blocking == 0


def test_golden_all_rungs_clean_completes_the_plan() -> None:
    rounds = (
        CLEAN_FULL_PLAN_ROUNDS[0],
        _round(EnumPlanReviewScope.FIRST_SLICE, ()),
        _round(
            EnumPlanReviewScope.LATER_RUNG, (_finding("nitpick", False),), rung="rung-a"
        ),
    )
    result = _expect(
        _request(rounds=rounds, later_rungs=("rung-a",)),
        EnumPlanReviewMove.PLAN_COMPLETE,
    )
    assert result.rule == "all_rungs_clean"
    assert result.next_scope is None
    assert result.next_rung is None
    assert result.open_blocking == 0


def test_golden_the_request_settings_win_over_the_injected_ones() -> None:
    handler = HandlerPlanReviewLadder(SETTINGS)
    rounds = (_round(EnumPlanReviewScope.FULL_PLAN, (_finding("style"),)),)
    under_request = handler.handle(_request(rounds=rounds, settings=OTHER_SETTINGS))
    assert under_request.move is EnumPlanReviewMove.FULL_PLAN_ROUND
    assert under_request.rule == "blocking_open", "style blocks under the request kinds"
    under_injected = handler.handle(_request(rounds=rounds))
    assert under_injected.move is EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE
    assert under_injected.rule == "no_blocking_left", "style is non-blocking injected"


# ----------------------------------------------------------------------------------------------- error


def test_error_either_refusal_returns_to_author_despite_a_clean_history() -> None:
    for verdict in (
        EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION,
        EnumPlanFirstSliceVerdict.EMPTY_FIRST_SLICE_SECTION,
    ):
        result = _expect(
            _request(verdict=verdict, rounds=CLEAN_FULL_PLAN_ROUNDS),
            EnumPlanReviewMove.RETURN_TO_AUTHOR,
        )
        assert result.rule == "first_slice_not_ok"
        assert result.next_scope is None
        assert result.open_blocking == 0


def test_error_a_later_rung_round_without_a_rung_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelPlanReviewRound(scope=EnumPlanReviewScope.LATER_RUNG)


def test_error_a_full_plan_round_carrying_a_rung_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelPlanReviewRound(scope=EnumPlanReviewScope.FULL_PLAN, rung="rung-a")


def test_error_an_empty_blocking_kinds_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelPlanReviewLadderSettings(blocking_kinds=())


# ------------------------------------------------------------------------------------------------ chain


def test_chain_the_golden_decision_emits_a_start_then_a_success() -> None:
    sink = InMemoryEmissionSink()
    rounds = (_round(EnumPlanReviewScope.FULL_PLAN, (_finding("style"),)),)
    result = _expect(
        _request(rounds=rounds), EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE
    )
    chain = ChainEmitter(
        sink,
        node="node_plan_review_ladder_compute",
        piece=9,
        correlation_id="ladder-golden",
        backend="in_memory",
    )
    chain.start()
    chain.success({"plans_decided": 1})
    assert [e.correlation_id for e in sink.events] == ["ladder-golden", "ladder-golden"]
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert sink.events[1].counters == {"plans_decided": 1}
    assert sink.events[1].failure_code is None
    assert result.move is EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE


def test_chain_the_refusal_emits_a_start_then_a_typed_failure() -> None:
    sink = InMemoryEmissionSink()
    result = _expect(
        _request(verdict=EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION),
        EnumPlanReviewMove.RETURN_TO_AUTHOR,
    )
    chain = ChainEmitter(
        sink,
        node="node_plan_review_ladder_compute",
        piece=9,
        correlation_id="ladder-refusal",
        backend="in_memory",
    )
    chain.start()
    chain.failure(result.move.value)
    assert [e.correlation_id for e in sink.events] == [
        "ladder-refusal",
        "ladder-refusal",
    ]
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == "return_to_author"


# ---------------------------------------------------------------------------------------------- settings


def test_settings_with_no_settings_in_the_request_the_overlay_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, NODE_NAME, {"blocking_kinds": ["style"]}
    )
    assert load_settings() == ModelPlanReviewLadderSettings(blocking_kinds=("style",))
    rounds = (_round(EnumPlanReviewScope.FULL_PLAN, (_finding("style"),)),)
    result = HandlerPlanReviewLadder().handle(_request(rounds=rounds, settings=None))
    assert result.move is EnumPlanReviewMove.FULL_PLAN_ROUND
    assert result.rule == "blocking_open", "style blocks under the overlay's kinds"


def test_settings_with_no_overlay_the_packaged_contract_config_is_in_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert load_settings() == ModelPlanReviewLadderSettings(
        blocking_kinds=BLOCKING_KINDS
    )
    rounds = (_round(EnumPlanReviewScope.FULL_PLAN, (_finding("wrong_fact"),)),)
    result = HandlerPlanReviewLadder().handle(_request(rounds=rounds, settings=None))
    assert result.move is EnumPlanReviewMove.FULL_PLAN_ROUND
    assert result.rule == "blocking_open"


# ---------------------------------------------------------------------------------------------- contract


def test_contract_declares_the_topics_its_handler_and_its_models() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_plan_review_ladder_compute"
    for side, model in (
        ("input_model", ModelPlanReviewLadderRequest),
        ("output_model", ModelPlanReviewLadderResult),
    ):
        declared = contract[side]
        module = importlib.import_module(declared["module"])
        assert getattr(module, declared["name"]) is model
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerPlanReviewLadder
    )
    dispatch = contract["runtime_dispatch"]
    assert (
        dispatch["command_topic"]
        == "onex.cmd.omnimarket.plan-review-ladder-decide-requested.v1"
    )
    terminal = dispatch["terminal_events"]
    assert (
        terminal["success"]
        == "onex.evt.omnimarket.plan-review-ladder-decide-completed.v1"
    )
    assert (
        terminal["failure"] == "onex.evt.omnimarket.plan-review-ladder-decide-failed.v1"
    )
    routing = contract["handler_routing"]["handlers"]
    assert [entry["operation"] for entry in routing] == ["decide_plan_review_ladder"]


def test_handler_does_no_io() -> None:
    tree = ast.parse(Path(handler_plan_review_ladder.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"os", "subprocess", "socket", "time", "random", "pathlib"}


# ------------------------------------------------------------------------------------------- determinism


def test_the_same_request_judged_twice_gives_the_same_result() -> None:
    handler = HandlerPlanReviewLadder(SETTINGS)
    rounds = (
        CLEAN_FULL_PLAN_ROUNDS[0],
        _round(EnumPlanReviewScope.FIRST_SLICE, ()),
        _round(
            EnumPlanReviewScope.LATER_RUNG, (_finding("nitpick", False),), rung="rung-a"
        ),
    )
    request = _request(rounds=rounds, later_rungs=("rung-a", "rung-b"))
    assert handler.handle(request) == handler.handle(request)
    empty = _request(rounds=(), settings=SETTINGS)
    assert handler.handle(empty) == handler.handle(empty)
    assert (
        request.model_dump_json()
        == _request(rounds=rounds, later_rungs=("rung-a", "rung-b")).model_dump_json()
    )


# ------------------------------------------------------------------------------------------------ seams


def test_seam_every_first_slice_verdict_is_judged_explicitly() -> None:
    """The first-slice check's enum is the shared shape: a new verdict fails this
    until the ladder's reading of it is chosen on purpose."""
    expected = {
        EnumPlanFirstSliceVerdict.OK: EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE,
        EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION: EnumPlanReviewMove.RETURN_TO_AUTHOR,
        EnumPlanFirstSliceVerdict.EMPTY_FIRST_SLICE_SECTION: EnumPlanReviewMove.RETURN_TO_AUTHOR,
    }
    assert set(expected) == set(EnumPlanFirstSliceVerdict)
    for verdict, move in expected.items():
        request = _request(verdict=verdict, rounds=CLEAN_FULL_PLAN_ROUNDS)
        assert HandlerPlanReviewLadder().handle(request).move is move


def test_seam_the_request_shape_the_plan_review_skill_sends_is_pinned() -> None:
    """The plan review set skill sends these field names; the skill's own test pins
    the same literals, so a rename on either side turns one of the two red."""
    assert set(ModelPlanReviewLadderRequest.model_fields) == {
        "first_slice_verdict",
        "rounds",
        "later_rungs",
        "settings",
    }
    assert set(ModelPlanReviewRound.model_fields) == {"scope", "rung", "findings"}
    assert set(ModelPlanReviewFinding.model_fields) == {"kind", "open"}
    assert {scope.value for scope in EnumPlanReviewScope} == {
        "full_plan",
        "first_slice",
        "later_rung",
    }
    assert {move.value for move in EnumPlanReviewMove} == {
        "return_to_author",
        "full_plan_round",
        "stop_full_plan_review_first_slice",
        "first_slice_round",
        "build_first_slice",
        "rung_round",
        "plan_complete",
    }


# ---------------------------------------------------------------------------------------- injected breaks


def test_break_counting_non_blocking_findings_as_blocking_turns_the_stop_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rounds = (_round(EnumPlanReviewScope.FULL_PLAN, (_finding("style"),)),)
    request = _request(rounds=rounds)
    # the real judge holds it
    _expect(request, EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE)

    real = handler_plan_review_ladder.check_plan_review_ladder

    def counts_every_open_finding(
        request: ModelPlanReviewLadderRequest,
        settings: ModelPlanReviewLadderSettings,
    ) -> ModelPlanReviewLadderResult:
        last = request.rounds[-1] if request.rounds else None
        if last is not None and any(finding.open for finding in last.findings):
            return ModelPlanReviewLadderResult(
                move=EnumPlanReviewMove.FULL_PLAN_ROUND,
                next_scope=EnumPlanReviewScope.FULL_PLAN,
                next_rung=None,
                open_blocking=99,
                rule="blocking_open",
                detail="the break: every open finding blocks",
            )
        return real(request, settings)

    monkeypatch.setattr(
        handler_plan_review_ladder,
        "check_plan_review_ladder",
        counts_every_open_finding,
    )
    with pytest.raises(AssertionError):
        _expect(request, EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE)


def test_break_ignoring_the_first_slice_verdict_turns_the_return_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(
        verdict=EnumPlanFirstSliceVerdict.EMPTY_FIRST_SLICE_SECTION,
        rounds=CLEAN_FULL_PLAN_ROUNDS,
    )
    # the real judge holds it
    _expect(request, EnumPlanReviewMove.RETURN_TO_AUTHOR)

    real = handler_plan_review_ladder.check_plan_review_ladder

    def ignores_the_verdict(
        request: ModelPlanReviewLadderRequest,
        settings: ModelPlanReviewLadderSettings,
    ) -> ModelPlanReviewLadderResult:
        accepted = request.model_copy(
            update={"first_slice_verdict": EnumPlanFirstSliceVerdict.OK}
        )
        return real(accepted, settings)

    monkeypatch.setattr(
        handler_plan_review_ladder,
        "check_plan_review_ladder",
        ignores_the_verdict,
    )
    with pytest.raises(AssertionError):
        _expect(request, EnumPlanReviewMove.RETURN_TO_AUTHOR)


def test_break_always_returning_full_plan_round_turns_the_stop_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rounds = (_round(EnumPlanReviewScope.FULL_PLAN, (_finding("style"),)),)
    request = _request(rounds=rounds)
    # the real judge holds it
    _expect(request, EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE)

    def always_another_full_plan_round(
        request: ModelPlanReviewLadderRequest,
        settings: ModelPlanReviewLadderSettings,
    ) -> ModelPlanReviewLadderResult:
        return ModelPlanReviewLadderResult(
            move=EnumPlanReviewMove.FULL_PLAN_ROUND,
            next_scope=EnumPlanReviewScope.FULL_PLAN,
            next_rung=None,
            open_blocking=99,
            rule="blocking_open",
            detail="the break: the whole plan is never done",
        )

    monkeypatch.setattr(
        handler_plan_review_ladder,
        "check_plan_review_ladder",
        always_another_full_plan_round,
    )
    with pytest.raises(AssertionError):
        _expect(request, EnumPlanReviewMove.STOP_FULL_PLAN_REVIEW_FIRST_SLICE)
