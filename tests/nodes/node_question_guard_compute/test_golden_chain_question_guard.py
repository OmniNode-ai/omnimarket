# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The question guard: a drafted reply's questions against the rulings in force.

AC3 (real options admitted) is falsified with recorded questions from RULING rows.
The refusal classes (a dev-lane restore or rebuild, work going to subagents) are
exercised here through the contract's rule data; their recorded-text falsifiers are
owed (see the PR body).
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_question_guard_compute import NodeQuestionGuardCompute
from omnimarket.nodes.node_question_guard_compute.handlers.handler_question_guard import (
    HandlerQuestionGuard,
)
from omnimarket.nodes.node_question_guard_compute.models import (
    EnumQuestionGuardVerdict,
    ModelQuestionGuardRequest,
    ModelQuestionGuardRule,
)
from omnimarket.nodes.node_question_guard_compute.overlay import (
    load_contract,
    load_question_guard_rules,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
RESTORE = "2026-10-20T17:35:21Z"
SUBAGENTS = "2026-10-15T11:37:28Z"
REAL_OPTIONS = "2026-10-10T17:54:23Z"
RULING_ROWS = tuple(
    f"{stamp} | RULING | lane=orchestrator | ticket=OMN-17427 | text=scrubbed"
    for stamp in (REAL_OPTIONS, SUBAGENTS, RESTORE)
)


def _recorded() -> dict[str, list[dict[str, str]]]:
    raw = yaml.safe_load(
        (FIXTURES / "recorded_questions.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(raw, dict)
    return raw


def _ask(text: str, rows: tuple[str, ...] = RULING_ROWS) -> ModelQuestionGuardRequest:
    return ModelQuestionGuardRequest(reply_text=text, ruling_rows=rows)


def test_rule_data_lives_in_the_contract_not_in_python() -> None:
    contract = load_contract()
    rules = contract["config"]["question_guard"]["rules"]
    assert {r["ruling"] for r in rules} == {RESTORE, SUBAGENTS, REAL_OPTIONS}
    source = inspect.getsource(sys.modules[HandlerQuestionGuard.__module__])
    for stamp in (RESTORE, SUBAGENTS, REAL_OPTIONS):
        assert stamp not in source, "a ruling stamp is hard-coded in the handler"


@pytest.mark.parametrize("case", _recorded()["real_options"], ids=lambda c: c["source"])
def test_ac3_a_recorded_question_with_real_options_is_admitted(
    case: dict[str, str],
) -> None:
    result = NodeQuestionGuardCompute().handle(_ask(case["text"]))
    assert result.verdict is EnumQuestionGuardVerdict.ADMIT, case["source"]
    assert result.ruling_id == REAL_OPTIONS
    assert result.questions
    assert result.questions[0].options >= 2


@pytest.mark.parametrize("case", _recorded()["no_choice"], ids=lambda c: c["source"])
def test_a_recorded_question_with_no_choice_matches_no_refusal(
    case: dict[str, str],
) -> None:
    result = NodeQuestionGuardCompute().handle(_ask(case["text"]))
    assert result.verdict is EnumQuestionGuardVerdict.ADMIT, case["source"]
    assert result.ruling_id == ""


# Refusal classes, through the contract's rule data. The strings below are rule
# mechanics only; the recorded-text falsifiers for AC1 and AC2 are owed.
RESTORE_ASK = "The dev lane is down. Should I restore the dev lane now?"
REBUILD_ASK = "Want me to rebuild the dev runtime on the merged head?"
SUBAGENT_ASK = "May I send the remaining five files to subagents on this Mac?"


@pytest.mark.parametrize("text", [RESTORE_ASK, REBUILD_ASK])
def test_ac1_rule_a_dev_lane_restore_question_is_refused(text: str) -> None:
    result = NodeQuestionGuardCompute().handle(_ask(text))
    assert result.verdict is EnumQuestionGuardVerdict.REFUSE
    assert result.ruling_id == RESTORE
    assert result.rule_id == "dev-lane-restore"
    assert "operator" in result.reason


def test_ac2_rule_a_subagent_question_is_refused() -> None:
    result = NodeQuestionGuardCompute().handle(_ask(SUBAGENT_ASK))
    assert result.verdict is EnumQuestionGuardVerdict.REFUSE
    assert result.ruling_id == SUBAGENTS
    assert result.rule_id == "subagents-allowed"


def test_a_refusal_needs_its_ruling_row_in_the_input() -> None:
    rows = tuple(r for r in RULING_ROWS if not r.startswith(RESTORE))
    result = NodeQuestionGuardCompute().handle(_ask(RESTORE_ASK, rows))
    assert result.verdict is EnumQuestionGuardVerdict.ADMIT
    assert result.ruling_id == ""


def test_a_restore_named_in_a_statement_is_not_a_question() -> None:
    text = "I restored the dev lane at 14:02Z; it is healthy."
    result = NodeQuestionGuardCompute().handle(_ask(text))
    assert result.verdict is EnumQuestionGuardVerdict.ADMIT
    assert result.questions == ()


def test_quoted_and_fenced_text_is_not_read_as_a_question() -> None:
    text = "> Should I restore the dev lane?\n```\nShould I rebuild the dev lane?\n```\nDone."
    result = NodeQuestionGuardCompute().handle(_ask(text))
    assert result.questions == ()


def test_one_refused_question_refuses_the_reply() -> None:
    text = f"{_recorded()['real_options'][1]['text']} Also: {RESTORE_ASK}"
    result = NodeQuestionGuardCompute().handle(_ask(text))
    assert result.verdict is EnumQuestionGuardVerdict.REFUSE
    assert result.ruling_id == RESTORE
    assert [q.verdict for q in result.questions].count(
        EnumQuestionGuardVerdict.ADMIT
    ) == 1


def test_injected_rules_replace_the_contract_rules() -> None:
    rules = load_question_guard_rules({})
    narrowed = rules.model_copy(
        update={"rules": tuple(r for r in rules.rules if r.id != "dev-lane-restore")}
    )
    result = HandlerQuestionGuard(rules=narrowed).handle(_ask(RESTORE_ASK))
    assert result.verdict is EnumQuestionGuardVerdict.ADMIT


def test_request_rules_take_precedence_over_injected_rules() -> None:
    rules = load_question_guard_rules({})
    only = ModelQuestionGuardRule(
        id="no-questions",
        ruling=REAL_OPTIONS,
        verdict=EnumQuestionGuardVerdict.REFUSE,
        reason="test rule: every question is refused",
        match_all=(r"\?",),
    )
    request = _ask("Do we also need a stop command?").model_copy(
        update={"rules": rules.model_copy(update={"rules": (only,)})}
    )
    result = HandlerQuestionGuard(rules=rules).handle(request)
    assert result.verdict is EnumQuestionGuardVerdict.REFUSE
    assert result.rule_id == "no-questions"


def test_the_overlay_replaces_the_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, "question_guard", {"rules": []})
    assert load_question_guard_rules().rules == ()
    result = HandlerQuestionGuard(rules=load_question_guard_rules()).handle(
        _ask(RESTORE_ASK)
    )
    assert result.verdict is EnumQuestionGuardVerdict.ADMIT


def test_a_broken_overlay_refuses_naming_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, "question_guard", {"rulez": []})
    with pytest.raises(NodeOverlayError, match="rulez"):
        load_question_guard_rules()


def test_an_invalid_rule_pattern_refuses_at_load() -> None:
    with pytest.raises(ValueError, match="pattern"):
        ModelQuestionGuardRule(
            id="bad",
            ruling=RESTORE,
            verdict=EnumQuestionGuardVerdict.REFUSE,
            reason="x",
            match_all=("(",),
        )


def test_golden_chain_node_question_guard_compute_refuses_a_restore_question() -> None:
    """Golden chain: the node entry point refuses a restore question by ruling."""
    request = ModelQuestionGuardRequest(
        reply_text=RESTORE_ASK,
        ruling_rows=(
            f"{RESTORE} | RULING | lane=orchestrator | ticket=OMN-17427 | text=scrubbed",
        ),
    )
    result = NodeQuestionGuardCompute().handle(request)
    assert result.verdict is EnumQuestionGuardVerdict.REFUSE
    assert result.ruling_id == RESTORE
    assert result.rule_id == "dev-lane-restore"
    assert result.questions


def test_golden_chain_node_question_guard_compute_admits_a_statement() -> None:
    """Golden chain: the node entry point admits a statement with no ruling."""
    request = ModelQuestionGuardRequest(
        reply_text="I restored the dev lane at 14:02Z; it is healthy.",
        ruling_rows=(
            f"{RESTORE} | RULING | lane=orchestrator | ticket=OMN-17427 | text=scrubbed",
        ),
    )
    result = NodeQuestionGuardCompute().handle(request)
    assert result.verdict is EnumQuestionGuardVerdict.ADMIT
    assert result.ruling_id == ""
    assert result.questions == ()
