# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the plan owner check (OMN-24445).

Golden: a live claim whose lane the rule names, or whose ticket the rule lists, answers OWNED
with the answered event, and two claims of one lane name one owner. Error: an unreadable or
not-moved read, a stale answer with no owner and a claim line the handler cannot read are
UNKNOWN with the refused event, never MISSING; a finding no rule answers is UNKNOWN. Overlay:
two rules for one finding are refused. Failing control: the named break ``_is_live`` always
true reads a claim past its window as an owner; without the patch the same answer is MISSING.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderEventKind,
    EnumReaderParity,
    EnumReaderQuery,
    EnumReaderStatus,
    ModelReaderResult,
)
from omnimarket.models.plan_owner_check import (
    EnumPlanOwnerVerdict,
    ModelPlanOwnerEvent,
    ModelPlanOwnerOverlay,
    ModelPlanOwnerRequest,
    ModelPlanOwnerRule,
)
from omnimarket.nodes.node_plan_owner_check_effect.handlers import (
    handler_plan_owner_check,
)
from omnimarket.nodes.node_plan_owner_check_effect.handlers.handler_plan_owner_check import (
    HandlerPlanOwnerCheck,
    load_topics,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 14, 15, 0, 0, tzinfo=UTC)
TOPICS = load_topics()
UNKNOWN_FIX = "read the open-claims answer and fix the read it names"
HEADING = (
    "open-claims since=beginning claims=2 open=2 "
    "ledger-newest=2026-11-14T14:59:30Z now=2026-11-14T15:00:00Z"
)


class ListSink:
    def __init__(self) -> None:
        self.events: list[ModelPlanOwnerEvent] = []

    def emit(self, event: ModelPlanOwnerEvent) -> None:
        self.events.append(event)


def claim_line(lane: str, newest: str, ticket: str = "none") -> str:
    """One OPEN-CLAIM line in the shape the query node renders."""
    return (
        f"OPEN-CLAIM lane={lane} claim=2026-11-14T14:00:00Z db:row-1 "
        f"ticket={ticket} newest-row={newest} kind=STATUS age=0h30m rows=4 "
        f"host=h051 run=r1 leases=none liveness=unknown"
    )


def node_answer(
    lines: tuple[str, ...], status: EnumReaderStatus = EnumReaderStatus.OK
) -> ModelReaderResult:
    answered = status in (EnumReaderStatus.OK, EnumReaderStatus.STALE)
    return ModelReaderResult(
        reader="cutover-owner",
        query=EnumReaderQuery.OPEN_CLAIMS,
        status=status,
        reason="test",
        source="node" if answered else "none",
        lines=lines if answered else (),
        matched=len(lines) if answered else 0,
        watermark=NOW - timedelta(seconds=20) if answered else None,
        watermark_age_s=20 if answered else None,
        max_watermark_age_s=900,
        parity=EnumReaderParity.UNREAD,
    )


def rule() -> ModelPlanOwnerRule:
    return ModelPlanOwnerRule(
        finding="cutover-plan-owner",
        lane_prefixes=("ledger-cut",),
        tickets=("OMN-19609",),
        live_window_minutes=180,
        why="no live lane CLAIM names the cutover plan",
        fix="start a ledger cutover lane",
        unknown_fix=UNKNOWN_FIX,
    )


def owner_overlay() -> ModelPlanOwnerOverlay:
    return ModelPlanOwnerOverlay(
        reader="cutover-owner", rules=(rule(),), journal_env="J"
    )


def handler() -> tuple[HandlerPlanOwnerCheck, ListSink]:
    sink = ListSink()
    return HandlerPlanOwnerCheck(owner_overlay(), sink, clock=lambda: NOW), sink


def request(
    answer: ModelReaderResult, finding: str = "cutover-plan-owner"
) -> ModelPlanOwnerRequest:
    return ModelPlanOwnerRequest(finding=finding, node_answer=answer)


def test_golden_a_live_claim_of_a_named_lane_answers_owned() -> None:
    h, sink = handler()
    verdict = h.handle(
        request(
            node_answer(
                (HEADING, claim_line("ledger-cut-6803", "2026-11-14T14:30:00Z"))
            )
        )
    )
    assert verdict.verdict is EnumPlanOwnerVerdict.OWNED
    assert verdict.owners == ("ledger-cut-6803",)
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.ANSWERED
    assert event.topic == TOPICS["plan_owner_answered"]


def test_golden_a_live_claim_of_a_listed_ticket_answers_owned() -> None:
    h, _ = handler()
    verdict = h.handle(
        request(
            node_answer(
                (HEADING, claim_line("other-lane", "2026-11-14T14:59:00Z", "OMN-19609"))
            )
        )
    )
    assert verdict.verdict is EnumPlanOwnerVerdict.OWNED
    assert verdict.owners == ("other-lane",)


def test_two_claims_of_one_lane_name_one_owner() -> None:
    h, _ = handler()
    verdict = h.handle(
        request(
            node_answer(
                (
                    HEADING,
                    claim_line("ledger-cut-6803", "2026-11-14T14:30:00Z"),
                    claim_line("ledger-cut-6803", "2026-11-14T14:45:00Z"),
                )
            )
        )
    )
    assert verdict.verdict is EnumPlanOwnerVerdict.OWNED
    assert verdict.owners == ("ledger-cut-6803",)


def test_golden_a_claim_past_the_window_answers_missing_with_the_why_and_fix() -> None:
    h, sink = handler()
    verdict = h.handle(
        request(
            node_answer(
                (HEADING, claim_line("ledger-cut-6803", "2026-11-14T09:00:00Z"))
            )
        )
    )
    assert verdict.verdict is EnumPlanOwnerVerdict.MISSING
    assert verdict.line() == (
        "MISSING cutover-plan-owner | no live lane CLAIM names the cutover plan"
        " | FIX: start a ledger cutover lane"
    )
    assert sink.events[0].kind is EnumReaderEventKind.ANSWERED


def test_claims_of_unrelated_lanes_are_missing() -> None:
    h, _ = handler()
    verdict = h.handle(
        request(
            node_answer(
                (
                    HEADING,
                    claim_line("other-lane", "2026-11-14T14:59:00Z"),
                    claim_line("landing-6803", "2026-11-14T14:59:00Z"),
                )
            )
        )
    )
    assert verdict.verdict is EnumPlanOwnerVerdict.MISSING


def test_an_answer_of_only_the_heading_line_is_missing() -> None:
    h, _ = handler()
    verdict = h.handle(request(node_answer((HEADING,))))
    assert verdict.verdict is EnumPlanOwnerVerdict.MISSING


def test_a_newest_row_in_the_future_counts_as_live() -> None:
    h, _ = handler()
    verdict = h.handle(
        request(
            node_answer(
                (HEADING, claim_line("ledger-cut-6803", "2026-11-14T16:00:00Z"))
            )
        )
    )
    assert verdict.verdict is EnumPlanOwnerVerdict.OWNED


def test_a_stale_answer_that_carries_an_owning_claim_is_still_owned() -> None:
    h, _ = handler()
    verdict = h.handle(
        request(
            node_answer(
                (HEADING, claim_line("ledger-cut-6803", "2026-11-14T14:30:00Z")),
                EnumReaderStatus.STALE,
            )
        )
    )
    assert verdict.verdict is EnumPlanOwnerVerdict.OWNED


@pytest.mark.parametrize(
    "status", [EnumReaderStatus.UNREADABLE, EnumReaderStatus.NOT_MOVED]
)
def test_error_chain_a_read_that_did_not_answer_is_unknown(
    status: EnumReaderStatus,
) -> None:
    h, sink = handler()
    verdict = h.handle(request(node_answer((), status)))
    assert verdict.verdict is EnumPlanOwnerVerdict.UNKNOWN
    assert verdict.reason == "node-read-failed:test"
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.REFUSED
    assert event.topic == TOPICS["plan_owner_refused"]
    assert verdict.line().endswith(UNKNOWN_FIX)


def test_error_chain_a_stale_answer_with_no_owner_is_unknown_never_missing() -> None:
    h, _ = handler()
    verdict = h.handle(request(node_answer((HEADING,), EnumReaderStatus.STALE)))
    assert verdict.verdict is EnumPlanOwnerVerdict.UNKNOWN
    assert verdict.reason == "watermark-stale-and-no-owner"


def test_error_chain_a_claim_line_without_newest_row_is_unknown_never_missing() -> None:
    h, _ = handler()
    broken = (
        "OPEN-CLAIM lane=ledger-cut-6803 claim=2026-11-14T14:00:00Z db:row-1 "
        "ticket=none kind=STATUS age=0h30m rows=4 host=h051 run=r1 leases=none "
        "liveness=unknown"
    )
    verdict = h.handle(request(node_answer((HEADING, broken))))
    assert verdict.verdict is EnumPlanOwnerVerdict.UNKNOWN
    assert verdict.reason == "unparseable-claim-line"


def test_error_chain_a_finding_without_a_rule_is_unknown() -> None:
    h, _ = handler()
    verdict = h.handle(request(node_answer((HEADING,)), finding="other-plan-owner"))
    assert verdict.verdict is EnumPlanOwnerVerdict.UNKNOWN
    assert verdict.reason == "no-rule-for-finding"


def test_break_live_window_always_true_reads_a_past_window_claim_as_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named break: with ``_is_live`` always true the MISSING claim above reads OWNED."""
    answer = node_answer(
        (HEADING, claim_line("ledger-cut-6803", "2026-11-14T09:00:00Z"))
    )
    assert handler()[0].handle(request(answer)).verdict is EnumPlanOwnerVerdict.MISSING
    monkeypatch.setattr(
        handler_plan_owner_check, "_is_live", lambda *_args, **_kwargs: True
    )
    assert handler()[0].handle(request(answer)).verdict is EnumPlanOwnerVerdict.OWNED


def test_the_overlay_refuses_two_rules_for_one_finding() -> None:
    with pytest.raises(ValueError, match="rule findings must be unique"):
        ModelPlanOwnerOverlay(
            reader="cutover-owner", rules=(rule(), rule()), journal_env="J"
        )
