# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer judge in shadow files its verdict beside the live judge's (OMN-22915, L10).

Golden chain: a row the live judge accepted, judged by the shadow on the text before it, agrees and the
record holds both verdicts. Error chain: a row the live judge accepted and the shadow refuses is filed
as a difference that names both sides and the refusal rule. Failing control: with the shadow's guards
replaced by an accept-everything judge the same row is filed as agreeing, and the difference is lost.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_judge import (
    judge_row,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_shadow_judge import (
    WorkLedgerShadowJudge,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    EnumJudgeVerdict,
    ModelJudgeVerdict,
    ModelShadowJudgeRequest,
)

pytestmark = pytest.mark.unit

LEDGER = "ROLLING_WORK_LEDGER.md"
HOLD = "2026-09-28T10:10:00Z | HOLD | lane=beta | id=2026-09-28T10:10:00Z-beta | surface=lab-dev | until=2026-09-28T12:00:00Z | reserved"
HOLD_OTHER_LANE = "2026-09-28T10:20:00Z | HOLD | lane=gamma | id=2026-09-28T10:20:00Z-gamma | surface=lab-dev | until=2026-09-28T12:00:00Z | reserved too"
HOLD_REUSED_ID = "2026-09-28T10:30:00Z | HOLD | lane=beta | id=2026-09-28T10:10:00Z-beta | surface=lab-x | until=2026-09-28T12:00:00Z | again"
STATUS = "2026-09-28T10:05:00Z | STATUS | lane=alpha | ticket=OMN-1 | progress"

ACCEPT = ModelJudgeVerdict(verdict=EnumJudgeVerdict.ACCEPT)


def _accept_everything(row: str, existing: str, ledger_name: str) -> ModelJudgeVerdict:
    return ACCEPT


def _request(
    row: str, existing: str, live: ModelJudgeVerdict
) -> ModelShadowJudgeRequest:
    return ModelShadowJudgeRequest(
        row_ref="req-1", row=row, existing=existing, ledger_name=LEDGER, live=live
    )


def test_golden_a_row_both_judges_accept_is_filed_agreeing() -> None:
    record = WorkLedgerShadowJudge().handle(_request(STATUS, "", ACCEPT))
    assert record.agrees is True
    assert record.difference is None
    assert record.live == ACCEPT
    assert record.shadow == ACCEPT


def test_a_row_both_judges_refuse_under_the_same_rule_is_filed_agreeing() -> None:
    existing = HOLD + "\n"
    live = judge_row(HOLD_REUSED_ID, existing, LEDGER)
    assert live.verdict is EnumJudgeVerdict.REFUSE
    record = WorkLedgerShadowJudge().handle(_request(HOLD_REUSED_ID, existing, live))
    assert record.agrees is True
    assert record.shadow.rule == live.rule


def test_error_a_row_the_live_judge_accepts_and_the_shadow_refuses_names_the_difference() -> (
    None
):
    record = WorkLedgerShadowJudge().handle(
        _request(HOLD_REUSED_ID, HOLD + "\n", ACCEPT)
    )
    assert record.agrees is False
    assert record.live.verdict is EnumJudgeVerdict.ACCEPT
    assert record.shadow.verdict is EnumJudgeVerdict.REFUSE
    assert record.shadow.rule is not None
    assert record.shadow.rule.startswith("OMN-19256 row grammar")
    assert (
        record.difference == f"live accepted; shadow refused under {record.shadow.rule}"
    )


def test_a_row_the_live_judge_refuses_and_the_shadow_accepts_names_the_difference() -> (
    None
):
    refused = ModelJudgeVerdict(
        verdict=EnumJudgeVerdict.REFUSE, rule="OMN-19256 row grammar", reason="r"
    )
    record = WorkLedgerShadowJudge().handle(_request(STATUS, "", refused))
    assert record.agrees is False
    assert (
        record.difference == "shadow accepted; live refused under OMN-19256 row grammar"
    )


def test_both_refusing_under_different_rules_is_a_difference() -> None:
    other = ModelJudgeVerdict(
        verdict=EnumJudgeVerdict.REFUSE, rule="OMN-1 other rule", reason="r"
    )
    record = WorkLedgerShadowJudge().handle(
        _request(HOLD_REUSED_ID, HOLD + "\n", other)
    )
    assert record.agrees is False
    assert record.difference is not None
    assert record.difference.startswith(
        "both refused; live rule OMN-1 other rule, shadow rule "
    )


def test_failing_control_a_shadow_with_its_guards_removed_loses_the_difference() -> (
    None
):
    broken = WorkLedgerShadowJudge(judge=_accept_everything)
    record = broken.handle(_request(HOLD_REUSED_ID, HOLD + "\n", ACCEPT))
    # The injected break: the same row the real shadow refuses is filed as agreeing.
    assert record.agrees is True
    real = WorkLedgerShadowJudge().handle(_request(HOLD_REUSED_ID, HOLD + "\n", ACCEPT))
    assert real.agrees is False


def test_observe_judges_each_row_on_the_rows_the_live_judge_accepted_before_it() -> (
    None
):
    shadow = WorkLedgerShadowJudge()
    first = shadow.observe(row_ref="a", row=HOLD, ledger_name=LEDGER, live=ACCEPT)
    assert first.agrees is True
    second = shadow.observe(
        row_ref="b", row=HOLD_OTHER_LANE, ledger_name=LEDGER, live=ACCEPT
    )
    # The surface is leased by the first HOLD, which the shadow's text now holds.
    assert second.agrees is False
    assert second.shadow.rule is not None
    assert second.shadow.rule.startswith("OMN-19261 surface lease")


def test_a_row_the_live_judge_refused_does_not_join_the_shadow_text() -> None:
    shadow = WorkLedgerShadowJudge()
    refused = ModelJudgeVerdict(verdict=EnumJudgeVerdict.REFUSE, rule="x", reason="x")
    shadow.observe(row_ref="a", row=HOLD, ledger_name=LEDGER, live=refused)
    follow = shadow.observe(
        row_ref="b", row=HOLD_OTHER_LANE, ledger_name=LEDGER, live=ACCEPT
    )
    assert follow.agrees is True
